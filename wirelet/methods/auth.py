from __future__ import annotations

import asyncio
import getpass
import logging
from typing import Optional

from wirelet import errors, raw
from wirelet.crypto.srp import compute_check
from wirelet.types import User

log = logging.getLogger(__name__)


class SentCode:
    """What :meth:`send_code` returns: keep ``phone_code_hash`` for :meth:`sign_in`."""

    def __init__(self, phone_code_hash: str, type: str, next_type: Optional[str], timeout: Optional[int]) -> None:
        self.phone_code_hash = phone_code_hash
        self.type = type
        self.next_type = next_type
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"SentCode(type={self.type!r}, next_type={self.next_type!r}, timeout={self.timeout})"


def _code_type(value) -> Optional[str]:
    if value is None:
        return None
    name = type(value).__name__
    for prefix in ("SentCodeType", "CodeType"):
        name = name.removeprefix(prefix)
    return name.lower()


class Auth:
    def _require_api(self) -> None:
        if not (self.api_id and self.api_hash):
            raise ValueError(
                "Logging in needs api_id and api_hash from https://my.telegram.org "
                "(Client('name', api_id=..., api_hash=...))"
            )

    async def _authorized(self, authorization) -> User:
        if isinstance(authorization, raw.types.auth.AuthorizationSignUpRequired):
            raise errors.PhoneNumberUnoccupied("PHONE_NUMBER_UNOCCUPIED", 400)
        user = authorization.user
        self.me = user
        self.storage.user_id = user.id
        self.storage.is_bot = bool(user.bot)
        await self.storage.save()
        return User._parse(self, user)

    async def sign_in_bot(self, bot_token: str) -> User:
        """Log in as the bot with this token from @BotFather."""
        self._require_api()
        authorization = await self.invoke(raw.functions.auth.ImportBotAuthorization(
            flags=0, api_id=self.api_id, api_hash=self.api_hash, bot_auth_token=bot_token,
        ))
        return await self._authorized(authorization)

    async def send_code(self, phone_number: str) -> SentCode:
        """Ask Telegram to send a login code to this phone number."""
        self._require_api()
        phone_number = phone_number.strip().replace(" ", "")
        sent = await self.invoke(raw.functions.auth.SendCode(
            phone_number=phone_number,
            api_id=self.api_id,
            api_hash=self.api_hash,
            settings=raw.types.CodeSettings(),
        ))
        if isinstance(sent, raw.types.auth.SentCodeSuccess):
            await self._authorized(sent.authorization)
            return SentCode("", "success", None, None)
        if isinstance(sent, raw.types.auth.SentCodePaymentRequired):
            raise errors.named("PaymentRequired")(
                "Telegram requires a payment to log in this number", 400)
        return SentCode(sent.phone_code_hash, _code_type(sent.type), _code_type(sent.next_type), sent.timeout)

    async def resend_code(self, phone_number: str, phone_code_hash: str) -> SentCode:
        sent = await self.invoke(raw.functions.auth.ResendCode(
            phone_number=phone_number, phone_code_hash=phone_code_hash))
        return SentCode(sent.phone_code_hash, _code_type(sent.type), _code_type(sent.next_type), sent.timeout)

    async def sign_in(self, phone_number: str, phone_code_hash: str, phone_code: str) -> User:
        """Finish logging in with the code. Raises :class:`errors.SessionPasswordNeeded`
        when two-step verification is on; then call :meth:`check_password`."""
        authorization = await self.invoke(raw.functions.auth.SignIn(
            phone_number=phone_number.strip().replace(" ", ""),
            phone_code_hash=phone_code_hash,
            phone_code=phone_code.strip(),
        ))
        return await self._authorized(authorization)

    async def sign_up(self, phone_number: str, phone_code_hash: str, first_name: str,
                      last_name: str = "") -> User:
        """Create a new account for a number that has none."""
        authorization = await self.invoke(raw.functions.auth.SignUp(
            phone_number=phone_number, phone_code_hash=phone_code_hash,
            first_name=first_name, last_name=last_name,
        ))
        return await self._authorized(authorization)

    async def check_password(self, password: str) -> User:
        """Log in with the two-step verification password."""
        info = await self.invoke(raw.functions.account.GetPassword())
        check = await asyncio.to_thread(compute_check, info, password)
        authorization = await self.invoke(raw.functions.auth.CheckPassword(password=check))
        return await self._authorized(authorization)

    async def log_out(self) -> None:
        """Log out and delete the session (the account stays, of course)."""
        await self.invoke(raw.functions.auth.LogOut())
        await self.stop()
        await self.storage.delete()

    async def get_me(self) -> User:
        self.me = await self._fetch_me()
        return User._parse(self, self.me)

    def export_session_string(self) -> str:
        """One line that logs this account in again: ``Client(..., session_string=...)``."""
        return self.storage.export_session_string()

    # -- interactive login used by start() --------------------------------- #

    async def _ask(self, prompt: str, secret: bool = False) -> str:
        reader = getpass.getpass if secret else input
        return (await asyncio.to_thread(reader, prompt)).strip()

    async def _authorize(self) -> None:
        if self.bot_token:
            await self.sign_in_bot(self.bot_token)
            return

        self._require_api()

        while True:
            phone = self.phone_number or await self._ask("Enter phone number or bot token: ")
            if ":" in phone and not phone.startswith("+"):
                try:
                    await self.sign_in_bot(phone)
                    return
                except errors.RPCError as e:
                    print(e)
                    self.phone_number = None
                    continue
            try:
                sent = await self.send_code(phone)
                break
            except (errors.PhoneNumberInvalid, errors.named("PhoneNumberBanned")) as e:
                print(e)
                self.phone_number = None

        if sent.type == "success":
            return
        print(f"The code was sent via {sent.type}.")

        while True:
            code = self.phone_code or await self._ask("Enter the code you received: ")
            try:
                await self.sign_in(phone, sent.phone_code_hash, code)
                return
            except (errors.PhoneCodeInvalid, errors.PhoneCodeExpired) as e:
                print(e)
                self.phone_code = None
                if isinstance(e, errors.PhoneCodeExpired):
                    sent = await self.send_code(phone)
            except errors.SessionPasswordNeeded:
                break
            except errors.PhoneNumberUnoccupied:
                print("This number has no Telegram account yet; let's create one.")
                first = await self._ask("First name: ")
                last = await self._ask("Last name (optional): ")
                await self.sign_up(phone, sent.phone_code_hash, first, last)
                return

        while True:
            password = self.password or await self._ask("Two-step verification password: ", secret=True)
            try:
                await self.check_password(password)
                return
            except errors.PasswordHashInvalid as e:
                print(e)
                self.password = None
