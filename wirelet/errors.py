"""Errors raised by wirelet.

Every Telegram RPC error is an :class:`RPCError`. Its class comes from the
error message, so any error Telegram sends can be caught by name, even one
newer than this release::

    from wirelet import errors

    try:
        await app.sign_in(phone, code_hash, code)
    except errors.PhoneCodeInvalid:
        ...
    except errors.FloodWait as e:
        await asyncio.sleep(e.value)

Errors also subclass their HTTP-like category (:class:`BadRequest`,
:class:`Flood`, ...) so broader ``except`` clauses work too.
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Tuple, Type


class WireletError(Exception):
    """Base class for every error this library raises."""


class SecurityError(WireletError):
    """The server sent data that failed an MTProto security check."""


class AuthKeyError(WireletError):
    """The authorization key could not be created."""


class RPCError(WireletError):
    """An error returned by Telegram for an API call."""

    CODE: int = 0
    NAME: str = "RPC_ERROR"

    def __init__(self, message: str = "", code: Optional[int] = None,
                 value: Optional[int] = None, request: Optional[str] = None) -> None:
        self.message = message or self.NAME
        self.code = code if code is not None else self.CODE
        self.value = value
        self.request = request
        where = f" (caused by {request})" if request else ""
        super().__init__(f"[{self.code} {self.message}]{where}")


class SeeOther(RPCError):
    CODE, NAME = 303, "SEE_OTHER"


class BadRequest(RPCError):
    CODE, NAME = 400, "BAD_REQUEST"


class Unauthorized(RPCError):
    CODE, NAME = 401, "UNAUTHORIZED"


class Forbidden(RPCError):
    CODE, NAME = 403, "FORBIDDEN"


class NotFound(RPCError):
    CODE, NAME = 404, "NOT_FOUND"


class NotAcceptable(RPCError):
    CODE, NAME = 406, "NOT_ACCEPTABLE"


class Flood(RPCError):
    CODE, NAME = 420, "FLOOD"


class InternalServerError(RPCError):
    CODE, NAME = 500, "INTERNAL"


class ServiceUnavailable(RPCError):
    CODE, NAME = 503, "SERVICE_UNAVAILABLE"


CATEGORIES: Dict[int, Type[RPCError]] = {
    c.CODE: c for c in (
        SeeOther, BadRequest, Unauthorized, Forbidden, NotFound,
        NotAcceptable, Flood, InternalServerError, ServiceUnavailable,
    )
}

_NUMBER = re.compile(r"_(\d+)(?=_|$)")
_named: Dict[str, Type[RPCError]] = {}
_combined: Dict[Tuple[str, int], Type[RPCError]] = {}


def _class_name(error_name: str) -> str:
    """``FLOOD_WAIT_X`` -> ``FloodWait``; ``PHONE_MIGRATE_X`` -> ``PhoneMigrate``."""
    parts = [p for p in error_name.split("_") if p and p != "X"]
    return "".join(p.capitalize() if not p.isdigit() else p for p in parts)


def _error_name(class_name: str) -> str:
    """``FloodWait`` -> ``FLOOD_WAIT``."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", class_name).upper()


def named(class_name: str) -> Type[RPCError]:
    """The class for an error, created the first time anyone asks for it."""
    cls = _named.get(class_name)
    if cls is None:
        cls = type(class_name, (RPCError,), {
            "NAME": _error_name(class_name),
            "__module__": __name__,
        })
        _named[class_name] = cls
    return cls


def from_rpc(code: int, message: str, request: Optional[str] = None) -> RPCError:
    """Build the exception for a ``rpc_error`` the server returned."""
    value = None
    match = _NUMBER.search(message)
    if match:
        value = int(match.group(1))
        message_pattern = message[:match.start()] + "_X" + message[match.end():]
    else:
        message_pattern = message

    # Some errors put the number up front, e.g. "2FA_CONFIRM_WAIT_X".
    name = _class_name(message_pattern)
    if not name or not name[0].isalpha():
        name = "Error" + name

    # -503 "Timeout" means Telegram's backend timed out: retryable, like a 503.
    category = CATEGORIES.get(code) or (ServiceUnavailable if code == -503 else RPCError)
    key = (name, code)
    cls = _combined.get(key)
    if cls is None:
        base = named(name)
        bases = (base, category) if category is not RPCError else (base,)
        cls = type(name, bases, {"__module__": __name__, "CODE": code})
        _combined[key] = cls

    return cls(message, code, value, request)


# Common errors, declared so editors can autocomplete them.
FloodWait = named("FloodWait")
FloodPremiumWait = named("FloodPremiumWait")
SlowmodeWait = named("SlowmodeWait")
SessionPasswordNeeded = named("SessionPasswordNeeded")
PhoneCodeInvalid = named("PhoneCodeInvalid")
PhoneCodeExpired = named("PhoneCodeExpired")
PhoneNumberInvalid = named("PhoneNumberInvalid")
PhoneNumberUnoccupied = named("PhoneNumberUnoccupied")
PasswordHashInvalid = named("PasswordHashInvalid")
AuthKeyUnregistered = named("AuthKeyUnregistered")
AuthKeyDuplicated = named("AuthKeyDuplicated")
SessionRevoked = named("SessionRevoked")
UserDeactivated = named("UserDeactivated")
AccessTokenInvalid = named("AccessTokenInvalid")
PeerIdInvalid = named("PeerIdInvalid")
UsernameNotOccupied = named("UsernameNotOccupied")
UsernameInvalid = named("UsernameInvalid")
MessageNotModified = named("MessageNotModified")
MessageIdInvalid = named("MessageIdInvalid")
ChatWriteForbidden = named("ChatWriteForbidden")
UserIsBlocked = named("UserIsBlocked")
FilePartMissing = named("FilePartMissing")
FileReferenceExpired = named("FileReferenceExpired")
PhoneMigrate = named("PhoneMigrate")
NetworkMigrate = named("NetworkMigrate")
UserMigrate = named("UserMigrate")
FileMigrate = named("FileMigrate")
StatsMigrate = named("StatsMigrate")


def __getattr__(name: str) -> Type[RPCError]:
    # ``errors.SomeNewError`` works for any CamelCase name.
    if name[:1].isupper():
        return named(name)
    raise AttributeError(name)
