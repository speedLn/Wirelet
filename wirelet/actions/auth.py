"""Logging in and out."""

from __future__ import annotations

import asyncio
import getpass
from typing import NamedTuple, Optional

from wirelet import faults, schema
from wirelet.crypto.srp import compute_check
from wirelet.model import User
from wirelet.model.people import user_from


class CodeTicket(NamedTuple):
    """Returned by :meth:`request_code`; pass it to :meth:`verify_code`."""

    phone: str
    hash: str
    via: str                # "app", "sms", "call", "email_code", ...
    next_via: Optional[str]
    wait: Optional[int]     # seconds before another code can be asked for


def _via(value) -> Optional[str]:
    if value is None:
        return None
    name = type(value).__name__
    for prefix in ("SentCodeType", "CodeType"):
        name = name.removeprefix(prefix)
    return "".join("_" + c.lower() if c.isupper() else c for c in name).lstrip("_")


def _clean_phone(phone: str) -> str:
    return "+" + "".join(ch for ch in phone if ch.isdigit())


class AuthActions:
    def _need_app(self) -> None:
        if not (self.app_id and self.app_hash):
            raise ValueError(
                "Logging in needs app_id and app_hash from https://my.telegram.org: "
                "Wire('name', app_id=..., app_hash=...)"
            )

    async def _signed_in(self, authorization) -> User:
        if isinstance(authorization, schema.objects.auth.AuthorizationSignUpRequired):
            raise faults.from_telegram(400, "PHONE_NUMBER_UNOCCUPIED", "auth.signIn")
        user = authorization.user
        self._me_raw = user
        self.storage.user_id = user.id
        self.storage.is_bot = bool(user.bot)
        await self.storage.save()
        return user_from(self, user)

    async def login_bot(self, token: str) -> User:
        """Sign in as the bot with this @BotFather token."""
        self._need_app()
        return await self._signed_in(await self.call(schema.methods.auth.ImportBotAuthorization(
            flags=0, api_id=self.app_id, api_hash=self.app_hash, bot_auth_token=token,
        )))

    async def request_code(self, phone: str) -> CodeTicket:
        """Have Telegram send a login code to ``phone``."""
        self._need_app()
        phone = _clean_phone(phone)
        sent = await self.call(schema.methods.auth.SendCode(
            phone_number=phone, api_id=self.app_id, api_hash=self.app_hash,
            settings=schema.objects.CodeSettings(),
        ))
        o = schema.objects
        if isinstance(sent, o.auth.SentCodeSuccess):
            await self._signed_in(sent.authorization)
            return CodeTicket(phone, "", "already_signed_in", None, None)
        if isinstance(sent, o.auth.SentCodePaymentRequired):
            raise faults.from_telegram(400, "PAYMENT_REQUIRED", "auth.sendCode")
        return CodeTicket(phone, sent.phone_code_hash, _via(sent.type), _via(sent.next_type), sent.timeout)

    async def resend_code(self, ticket: CodeTicket) -> CodeTicket:
        """Ask for the code again, usually by the ticket's ``next_via``."""
        sent = await self.call(schema.methods.auth.ResendCode(
            phone_number=ticket.phone, phone_code_hash=ticket.hash))
        return CodeTicket(ticket.phone, sent.phone_code_hash, _via(sent.type), _via(sent.next_type), sent.timeout)

    async def verify_code(self, ticket: CodeTicket, code: str) -> User:
        """Finish logging in with the code.

        Raises ``faults.SESSION_PASSWORD_NEEDED`` when two-step verification is
        on (then call :meth:`verify_password`), and
        ``faults.PHONE_NUMBER_UNOCCUPIED`` for a number without an account
        (then call :meth:`register`).
        """
        return await self._signed_in(await self.call(schema.methods.auth.SignIn(
            phone_number=ticket.phone, phone_code_hash=ticket.hash, phone_code=code.strip(),
        )))

    async def verify_password(self, password: str) -> User:
        """Finish logging in with the two-step verification password."""
        info = await self.call(schema.methods.account.GetPassword())
        check = await asyncio.to_thread(compute_check, info, password)
        return await self._signed_in(await self.call(schema.methods.auth.CheckPassword(password=check)))

    async def register(self, ticket: CodeTicket, first: str, last: str = "") -> User:
        """Create an account for a number that has none."""
        return await self._signed_in(await self.call(schema.methods.auth.SignUp(
            phone_number=ticket.phone, phone_code_hash=ticket.hash, first_name=first, last_name=last,
        )))

    async def logout(self) -> None:
        """End this login everywhere and delete its local file."""
        await self.call(schema.methods.auth.LogOut())
        await self.stop()
        await self.storage.delete()

    def export_key(self) -> str:
        """This login as one line, for ``Wire(key=...)``. Keep it secret: it *is* the login."""
        return self.storage.export_key()

    # -- the interactive login used by start() --------------------------- #

    async def _prompt(self, text: str, secret: bool = False) -> str:
        reader = getpass.getpass if secret else input
        return (await asyncio.to_thread(reader, text)).strip()

    async def _login(self) -> None:
        if self.bot:
            await self.login_bot(self.bot)
            return
        self._need_app()

        while True:
            answer = self.phone or await self._prompt("Phone number (or bot token): ")
            if ":" in answer and not answer.startswith("+"):
                try:
                    await self.login_bot(answer)
                    return
                except faults.TelegramFault as e:
                    print(e)
                    continue
            try:
                ticket = await self.request_code(answer)
                break
            except (faults.PHONE_NUMBER_INVALID, faults.PHONE_NUMBER_BANNED) as e:
                print(e)
                self.phone = None

        if ticket.via == "already_signed_in":
            return
        print(f"Code sent via {ticket.via}.")

        while True:
            code = self.code or await self._prompt("Code: ")
            try:
                await self.verify_code(ticket, code)
                return
            except faults.PHONE_CODE_INVALID as e:
                print(e)
                self.code = None
            except faults.PHONE_CODE_EXPIRED as e:
                print(e)
                self.code = None
                ticket = await self.request_code(ticket.phone)
            except faults.SESSION_PASSWORD_NEEDED:
                break
            except faults.PHONE_NUMBER_UNOCCUPIED:
                print("No account uses this number yet; creating one.")
                await self.register(ticket, await self._prompt("First name: "),
                                    await self._prompt("Last name (optional): "))
                return

        while True:
            password = self.password or await self._prompt("Two-step verification password: ", secret=True)
            try:
                await self.verify_password(password)
                return
            except faults.PASSWORD_HASH_INVALID as e:
                print(e)
                self.password = None
