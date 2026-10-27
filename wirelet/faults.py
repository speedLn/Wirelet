"""Everything that can go wrong, as exceptions.

Telegram's own errors use Telegram's exact names, so the docs at
https://core.telegram.org/api/errors map one to one::

    from wirelet import faults

    try:
        await wire.send(chat, "hi")
    except faults.FLOOD_WAIT as e:
        print("slow down for", e.amount, "seconds")
    except faults.CHAT_WRITE_FORBIDDEN:
        ...

Any name works, even errors newer than this release. Numbered errors drop
the number (``FLOOD_WAIT_30`` is ``faults.FLOOD_WAIT`` with ``amount == 30``).
Each error is also one of the broad groups below, by HTTP-like code.
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Tuple, Type


class Fault(Exception):
    """Base of every wirelet exception."""


class SecurityFault(Fault):
    """Data from the server failed an MTProto security check."""


class HandshakeFault(Fault):
    """No authorization key could be agreed with a data center."""


class TelegramFault(Fault):
    """Telegram refused a call.

    ``code`` is the numeric class, ``message`` the full text (``FLOOD_WAIT_30``),
    ``name`` the class name (``FLOOD_WAIT``), ``amount`` the number inside the
    message if any, ``method`` the API method that failed.
    """

    CODE: int = 0
    NAME: str = "TELEGRAM_FAULT"

    def __init__(self, message: str = "", code: Optional[int] = None,
                 amount: Optional[int] = None, method: Optional[str] = None) -> None:
        self.message = message or self.NAME
        self.code = self.CODE if code is None else code
        self.amount = amount
        self.method = method
        self.name = type(self).NAME
        super().__init__(f"{self.message} ({self.code})" + (f" in {method}" if method else ""))


# Broad groups, by code.
class Redirect(TelegramFault):          # 303: go to another data center
    CODE, NAME = 303, "REDIRECT"


class Rejected(TelegramFault):          # 400: something in the request is wrong
    CODE, NAME = 400, "REJECTED"


class NotSignedIn(TelegramFault):       # 401: login missing, expired or incomplete
    CODE, NAME = 401, "NOT_SIGNED_IN"


class Denied(TelegramFault):            # 403: not allowed
    CODE, NAME = 403, "DENIED"


class Missing(TelegramFault):           # 404
    CODE, NAME = 404, "MISSING"


class Refused(TelegramFault):           # 406: refused, often shown to the user by apps
    CODE, NAME = 406, "REFUSED"


class TooFast(TelegramFault):           # 420: rate limited
    CODE, NAME = 420, "TOO_FAST"


class ServerTrouble(TelegramFault):     # 500, 503, -503: worth retrying
    CODE, NAME = 500, "SERVER_TROUBLE"


GROUPS: Dict[int, Type[TelegramFault]] = {
    303: Redirect, 400: Rejected, 401: NotSignedIn, 403: Denied, 404: Missing,
    406: Refused, 420: TooFast, 500: ServerTrouble, 503: ServerTrouble, -503: ServerTrouble,
}

_NUMBER = re.compile(r"_(\d+)(?=_|$)")
_by_name: Dict[str, Type[TelegramFault]] = {}
_concrete: Dict[Tuple[str, int], Type[TelegramFault]] = {}


def named(name: str) -> Type[TelegramFault]:
    """The class for ``FLOOD_WAIT``-style names, made the first time it's needed."""
    name = name.upper()
    cls = _by_name.get(name)
    if cls is None:
        cls = type(name, (TelegramFault,), {"NAME": name, "__module__": __name__})
        _by_name[name] = cls
    return cls


def from_telegram(code: int, message: str, method: Optional[str] = None) -> TelegramFault:
    """Build the exception for an ``rpc_error`` Telegram sent."""
    amount = None
    found = _NUMBER.search(message)
    if found:
        amount = int(found.group(1))
        name = message[:found.start()] + message[found.end():]
    else:
        name = message
    name = name.strip("_") or "UNKNOWN"

    key = (name, code)
    cls = _concrete.get(key)
    if cls is None:
        group = GROUPS.get(code)
        bases = (named(name), group) if group else (named(name),)
        cls = type(name, bases, {"__module__": __name__, "CODE": code, "NAME": name})
        _concrete[key] = cls
    return cls(message, code, amount, method)


def __getattr__(name: str) -> Type[TelegramFault]:
    # faults.ANY_TELEGRAM_ERROR_NAME
    if name.isupper() and not name.startswith("_"):
        return named(name)
    raise AttributeError(name)
