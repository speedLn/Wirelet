"""Keyword constraints that decide which updates reach a listener.

Every ``wire.on.*`` decorator and ``wire.command`` takes these keywords; all
given ones must hold:

``chat``
    ``"dm"`` (private chats, bots included), ``"bot"``, ``"group"`` (groups and
    supergroups), ``"supergroup"``, ``"channel"``, a chat id, ``"@handle"``, or
    a list meaning any of them.
``by``
    Who sent it: ``"me"``, ``"bot"``, ``"human"``, a user id, ``"@handle"``, or a list.
``text``
    Exact text, or a list of accepted texts.
``pattern``
    A regex searched in the text (or button data / inline text). The match is
    put on the update as ``.match``.
``has``
    What the message must have: ``"media"``, ``"text"``, ``"reply"``,
    ``"forward"``, ``"buttons"``, ``"action"``, or a media kind such as
    ``"photo"``, ``"voice"``, ``"poll"``. A list means all of them.
``mine``
    True for messages this account sent, False for messages it received.
``data``
    For presses: the exact button data, or a list.
``when``
    Any function ``f(update) -> bool`` (may be async), or a list of them.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Callable, Iterable, List, Optional, Pattern, Union

from wirelet.model import Inline, Message, Press

Spec = Union[str, int, Iterable[Union[str, int]], None]


def _many(value) -> List:
    if value is None:
        return []
    if isinstance(value, (str, int, bytes, re.Pattern)) or callable(value):
        return [value]
    return list(value)


def _norm_handle(value: str) -> str:
    return value.lower().lstrip("@")


CHAT_KINDS = {
    "dm": ("dm", "bot"), "private": ("dm", "bot"), "bot": ("bot",),
    "group": ("group", "supergroup"), "supergroup": ("supergroup",), "channel": ("channel",),
}


def _chat_of(update):
    if isinstance(update, Message):
        return update.chat
    if isinstance(update, Press):
        return update.chat
    return None


def _author_of(update):
    return getattr(update, "author", None)


def _text_of(update) -> Optional[str]:
    if isinstance(update, Message):
        return update.text
    if isinstance(update, Inline):
        return update.text
    if isinstance(update, Press):
        return update.data if isinstance(update.data, str) else None
    return None


def _has(update: Message, what: str) -> bool:
    what = what.lower()
    if what == "media":
        return update.media is not None
    if what == "text":
        return bool(update.text) and update.media is None
    if what == "reply":
        return update.reply_to is not None
    if what in ("forward", "forwarded"):
        return update.forwarded_at is not None
    if what == "buttons":
        return update.buttons is not None
    if what == "action":
        return update.action is not None
    return update.media is not None and update.media.kind == what


class Constraints:
    """A compiled set of keyword constraints."""

    def __init__(self, *, chat: Spec = None, by: Spec = None, text=None,
                 pattern: Union[str, Pattern, None] = None, has: Spec = None,
                 mine: Optional[bool] = None, data=None,
                 when: Union[Callable, Iterable[Callable], None] = None) -> None:
        self.chats = _many(chat)
        self.authors = _many(by)
        self.texts = _many(text)
        self.pattern = re.compile(pattern) if isinstance(pattern, str) else pattern
        self.has = [h for h in _many(has)]
        self.mine = mine
        self.data = _many(data)
        self.when = _many(when)

    async def __call__(self, wire, update: Any) -> bool:
        if self.mine is not None:
            if not isinstance(update, Message) or update.from_me != self.mine:
                return False

        if self.chats and not self._chat_ok(_chat_of(update)):
            return False
        if self.authors and not self._author_ok(wire, _author_of(update)):
            return False

        if self.texts:
            if _text_of(update) not in self.texts:
                return False

        if self.data:
            if not isinstance(update, Press) or update.data not in self.data:
                return False

        if self.has:
            if not isinstance(update, Message) or not all(_has(update, h) for h in self.has):
                return False

        if self.pattern is not None:
            value = _text_of(update)
            found = self.pattern.search(value) if value is not None else None
            if found is None:
                return False
            update.match = found

        for check in self.when:
            result = check(update)
            if inspect.isawaitable(result):
                result = await result
            if not result:
                return False
        return True

    def _chat_ok(self, chat) -> bool:
        if chat is None:
            return False
        for spec in self.chats:
            if isinstance(spec, int):
                if chat.id == spec:
                    return True
            elif spec.lower() in CHAT_KINDS:
                if chat.kind in CHAT_KINDS[spec.lower()]:
                    return True
            elif chat.handle and chat.handle.lower() == _norm_handle(spec):
                return True
        return False

    def _author_ok(self, wire, author) -> bool:
        if author is None:
            return False
        for spec in self.authors:
            if isinstance(spec, int):
                if author.id == spec:
                    return True
            elif spec == "me":
                if author.id == wire.me_id:
                    return True
            elif spec == "bot":
                if author.bot:
                    return True
            elif spec == "human":
                if not author.bot:
                    return True
            elif author.handle and author.handle.lower() == _norm_handle(spec):
                return True
        return False


_ARGS = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')


class Command:
    """Matches ``<prefix><name> args...`` (and ``/name@ThisBot``)."""

    def __init__(self, names: Union[str, Iterable[str]], prefix: Union[str, Iterable[str]] = "/",
                 case: bool = False) -> None:
        self.names = {n if case else n.lower() for n in _many(names)}
        self.prefixes = sorted(_many(prefix), key=len, reverse=True)
        self.case = case

    async def __call__(self, wire, update: Any) -> bool:
        if not isinstance(update, Message) or not update.text:
            return False
        text = update.text
        for prefix in self.prefixes:
            if not text.startswith(prefix):
                continue
            head, _, rest = text[len(prefix):].partition(" ")
            head, _, newline_rest = head.partition("\n")
            if newline_rest:
                rest = newline_rest + (" " + rest if rest else "")
            name, _, target = head.partition("@")
            if target and wire.me_handle and target.lower() != wire.me_handle.lower():
                return False
            key = name if self.case else name.lower()
            if key in self.names:
                update.cmd = key
                update.rest = rest.strip()
                update.args = [a.replace('\\"', '"') if a or not b else b
                               for a, b in _ARGS.findall(update.rest)]
                return True
        return False
