"""User-account login: DC migration, login code and two-step verification.

The server side verifies the SRP proof the same way Telegram does, so a wrong
implementation of https://core.telegram.org/api/srp fails here.
"""

import hashlib
import os
import time

import pytest

from wirelet import Wire, faults, schema
from wirelet.crypto.math import KNOWN_DH_PRIME as P
from wirelet.crypto.srp import password_hash
from wirelet.network import dc as dcmod

from .mtproto_server import MockServer, install_test_key

PHONE, CODE, PASSWORD = "+15550001234", "12345", "correct horse"
ME = schema.objects.User(id=999, is_self=True, first_name="Me", access_hash=7, phone=PHONE[1:])


def H(*parts):
    return hashlib.sha256(b"".join(parts)).digest()


def pad(x):
    return x.to_bytes(256, "big")


class SRPServer:
    """Telegram's half of SRP-2048."""

    def __init__(self, password: str) -> None:
        self.g = 3
        self.salt1, self.salt2 = os.urandom(40), os.urandom(32)
        x = int.from_bytes(password_hash(password, self.salt1, self.salt2), "big")
        self.v = pow(self.g, x, P)
        self.b = int.from_bytes(os.urandom(256), "big")
        k = int.from_bytes(H(pad(P), pad(self.g)), "big")
        self.B = (k * self.v + pow(self.g, self.b, P)) % P
        self.srp_id = 4242

    def info(self):
        t = schema.objects
        return t.account.Password(
            has_password=True,
            current_algo=t.PasswordKdfAlgoSHA256SHA256PBKDF2HMACSHA512iter100000SHA256ModPow(
                salt1=self.salt1, salt2=self.salt2, g=self.g, p=pad(P)),
            srp_B=pad(self.B), srp_id=self.srp_id,
            new_algo=t.PasswordKdfAlgoUnknown(), new_secure_algo=t.SecurePasswordKdfAlgoUnknown(),
            secure_random=os.urandom(32),
        )

    def verify(self, check) -> bool:
        A = int.from_bytes(check.A, "big")
        u = int.from_bytes(H(pad(A), pad(self.B)), "big")
        S = pow(A * pow(self.v, u, P) % P, self.b, P)
        K = H(pad(S))
        hp, hg = H(pad(P)), H(pad(self.g))
        expected = H(bytes(a ^ b for a, b in zip(hp, hg)), H(self.salt1), H(self.salt2),
                     pad(A), pad(self.B), K)
        return check.srp_id == self.srp_id and check.M1 == expected


class Telegram:
    def __init__(self) -> None:
        self.srp = SRPServer(PASSWORD)
        self.migrated = False

    def __call__(self, query, conn):
        f, t = schema.methods, schema.objects
        if isinstance(query, f.help.GetConfig):
            return t.DataJSON(data="{}")
        if isinstance(query, f.auth.SendCode):
            if not self.migrated:
                self.migrated = True
                return t.RpcError(error_code=303, error_message="PHONE_MIGRATE_4")
            assert query.phone_number == PHONE and query.api_hash == "hash"
            return t.auth.SentCode(type=t.auth.SentCodeTypeApp(length=5), phone_code_hash="h4sh")
        if isinstance(query, f.auth.SignIn):
            if query.phone_code != CODE:
                return t.RpcError(error_code=400, error_message="PHONE_CODE_INVALID")
            return t.RpcError(error_code=401, error_message="SESSION_PASSWORD_NEEDED")
        if isinstance(query, f.account.GetPassword):
            return self.srp.info()
        if isinstance(query, f.auth.CheckPassword):
            if not self.srp.verify(query.password):
                return t.RpcError(error_code=400, error_message="PASSWORD_HASH_INVALID")
            return t.auth.Authorization(user=ME)
        if isinstance(query, f.users.GetUsers):
            return [ME]
        if isinstance(query, f.updates.GetState):
            return t.updates.State(pts=1, qts=0, date=int(time.time()), seq=0, unread_count=0)
        return t.RpcError(error_code=400, error_message="METHOD_NOT_MOCKED")


@pytest.fixture
async def env(monkeypatch, tmp_path):
    install_test_key(monkeypatch)
    telegram = Telegram()
    server = await MockServer(telegram).start()
    addr = [("127.0.0.1", server.port)]
    monkeypatch.setattr(dcmod, "PRODUCTION", {2: addr, 4: addr})
    yield server, telegram, tmp_path
    await server.stop()


async def test_login_with_code_and_password_after_migration(env):
    server, telegram, tmp_path = env
    async with Wire("me", app_id=1, app_hash="hash", phone=PHONE, code=CODE,
                    password=PASSWORD, folder=tmp_path) as wire:
        assert wire.me.id == 999 and not wire.is_bot and wire.me.is_me
        # Started on DC2, moved to DC4 when Telegram said PHONE_MIGRATE_4.
        assert server.seen_dc_ids == [2, 4]
        assert wire.storage.dc_id == 4
        assert any(isinstance(q, schema.methods.auth.CheckPassword) for q in server.received)


async def test_step_by_step_login_and_wrong_password(env):
    server, telegram, tmp_path = env
    wire = Wire("me2", app_id=1, app_hash="hash", folder=tmp_path, listen=False)
    await wire.connect()
    try:
        ticket = await wire.request_code("+1 555 000 1234")
        assert (ticket.phone, ticket.via, ticket.hash) == (PHONE, "app", "h4sh")

        with pytest.raises(faults.PHONE_CODE_INVALID):
            await wire.verify_code(ticket, "00000")
        with pytest.raises(faults.SESSION_PASSWORD_NEEDED) as caught:
            await wire.verify_code(ticket, CODE)
        assert isinstance(caught.value, faults.NotSignedIn)

        with pytest.raises(faults.PASSWORD_HASH_INVALID):
            await wire.verify_password("wrong password")
        user = await wire.verify_password(PASSWORD)
        assert user.id == 999 and user.phone == PHONE[1:]
    finally:
        await wire.disconnect()
