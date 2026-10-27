"""Filters decide which updates reach a handler.

Combine them with ``&``, ``|`` and ``~``::

    @app.on_message(filters.command("start") & filters.private)
    async def start(client, message): ...

Write your own with :func:`create`::

    is_long = filters.create(lambda _, __, m: len(m.text or "") > 100)
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Callable, Iterable, Optional, Pattern, Union

from wirelet.types import CallbackQuery, InlineQuery, Message


class Filter:
    async def __call__(self, client, update) -> bool:
        raise NotImplementedError

    def __and__(self, other: "Filter") -> "Filter":
        return _And(self, other)

    def __or__(self, other: "Filter") -> "Filter":
        return _Or(self, other)

    def __invert__(self) -> "Filter":
        return _Not(self)


async def _run(f: Union[Filter, Callable], client, update) -> bool:
    result = f(client, update)
    if inspect.isawaitable(result):
        result = await result
    return bool(result)


class _And(Filter):
    def __init__(self, a, b) -> None:
        self.a, self.b = a, b

    async def __call__(self, client, update) -> bool:
        return await _run(self.a, client, update) and await _run(self.b, client, update)


class _Or(Filter):
    def __init__(self, a, b) -> None:
        self.a, self.b = a, b

    async def __call__(self, client, update) -> bool:
        return await _run(self.a, client, update) or await _run(self.b, client, update)


class _Not(Filter):
    def __init__(self, f) -> None:
        self.f = f

    async def __call__(self, client, update) -> bool:
        return not await _run(self.f, client, update)


class _Custom(Filter):
    def __init__(self, func: Callable, name: str, **kwargs: Any) -> None:
        self.func = func
        self.name = name
        for key, value in kwargs.items():
            setattr(self, key, value)

    async def __call__(self, client, update) -> bool:
        result = self.func(self, client, update)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    def __repr__(self) -> str:
        return f"<Filter {self.name}>"


def create(func: Callable, name: Optional[str] = None, **kwargs: Any) -> Filter:
    """Make a filter from ``func(filter, client, update) -> bool`` (sync or async).

    Extra keyword arguments become attributes of the filter, available to
    ``func`` through its first argument.
    """
    return _Custom(func, name or getattr(func, "__name__", "CustomFilter"), **kwargs)


def _msg(update) -> Optional[Message]:
    if isinstance(update, Message):
        return update
    if isinstance(update, CallbackQuery):
        return update.message
    return None


# --------------------------------------------------------------------------- #
# Ready-made filters
# --------------------------------------------------------------------------- #

all = create(lambda _, __, ___: True, "all")

text = create(lambda _, __, u: isinstance(u, Message) and u.text is not None, "text")
media = create(lambda _, __, u: isinstance(u, Message) and u.media is not None, "media")
caption = create(lambda _, __, u: isinstance(u, Message) and u.caption is not None, "caption")
service = create(lambda _, __, u: isinstance(u, Message) and u.service is not None, "service")


def _media(kind: str) -> Filter:
    return create(lambda _, __, u: isinstance(u, Message) and u.media == kind, kind)


photo = _media("photo")
video = _media("video")
video_note = _media("video_note")
animation = _media("animation")
audio = _media("audio")
voice = _media("voice")
sticker = _media("sticker")
document = _media("document")
poll = _media("poll")
contact = _media("contact")
location = create(lambda _, __, u: isinstance(u, Message) and u.media in ("geo", "geolive", "venue"), "location")


def _chat_type(*types: str) -> Filter:
    return create(lambda _, __, u: (m := _msg(u)) is not None and m.chat is not None and m.chat.type in types,
                  "_".join(types))


private = _chat_type("private", "bot")
group = _chat_type("group", "supergroup")
channel = _chat_type("channel")

incoming = create(lambda _, __, u: isinstance(u, Message) and not u.outgoing, "incoming")
outgoing = create(lambda _, __, u: isinstance(u, Message) and u.outgoing, "outgoing")
me = create(lambda _, c, u: (m := _msg(u)) is not None and m.from_user is not None and m.from_user.id == c.me_id, "me")
bot = create(lambda _, __, u: (m := _msg(u)) is not None and m.from_user is not None and m.from_user.is_bot, "bot")
reply = create(lambda _, __, u: isinstance(u, Message) and u.reply_to_message_id is not None, "reply")
forwarded = create(lambda _, __, u: isinstance(u, Message) and u.forward_date is not None, "forwarded")
mentioned = create(lambda _, __, u: isinstance(u, Message) and u.mentioned, "mentioned")
edited = create(lambda _, __, u: isinstance(u, Message) and u.edit_date is not None, "edited")
via_bot = create(lambda _, __, u: isinstance(u, Message) and u.via_bot_id is not None, "via_bot")
media_group = create(lambda _, __, u: isinstance(u, Message) and u.media_group_id is not None, "media_group")


def command(commands: Union[str, Iterable[str]], prefixes: Union[str, Iterable[str]] = "/",
            case_sensitive: bool = False) -> Filter:
    """Match ``/command arg1 "arg two"``; sets ``message.command`` to the parts.

    ``/command@YourBot`` is matched too, but only when addressed to this bot.
    """
    names = {commands} if isinstance(commands, str) else set(commands)
    if not case_sensitive:
        names = {n.lower() for n in names}
    prefix_list = [prefixes] if isinstance(prefixes, str) else list(prefixes)
    args_re = re.compile(r'"([^"]*)"|(\S+)')

    async def func(flt, client, update) -> bool:
        if not isinstance(update, Message):
            return False
        content = update.content
        if not content:
            return False

        for prefix in prefix_list:
            if not content.startswith(prefix):
                continue
            head, _, rest = content[len(prefix):].partition(" ")
            name, _, target = head.partition("@")
            if target and client.me_username and target.lower() != client.me_username.lower():
                return False
            key = name if case_sensitive else name.lower()
            if key in names:
                update.command = [key] + [a or b for a, b in args_re.findall(rest)]
                return True
        return False

    return create(func, "command", commands=names, prefixes=prefix_list)


def regex(pattern: Union[str, Pattern], flags: int = 0) -> Filter:
    """Match message text, callback data or inline query text; sets ``.matches``."""
    compiled = re.compile(pattern, flags) if isinstance(pattern, str) else pattern

    def func(flt, client, update) -> bool:
        if isinstance(update, Message):
            value = update.content
        elif isinstance(update, CallbackQuery):
            value = update.data if isinstance(update.data, str) else None
        elif isinstance(update, InlineQuery):
            value = update.query
        else:
            return False
        if value is None:
            return False
        update.matches = list(compiled.finditer(value)) or None
        return bool(update.matches)

    return create(func, "regex", pattern=compiled)


def _ids(values) -> set:
    if isinstance(values, (int, str)):
        values = [values]
    out = set()
    for v in values:
        out.add(v.lower().lstrip("@") if isinstance(v, str) else v)
    return out


def user(users: Union[int, str, Iterable[Union[int, str]]]) -> Filter:
    """Updates from these user ids or usernames. ``"me"`` means yourself."""
    wanted = _ids(users)

    def func(flt, client, update) -> bool:
        sender = getattr(update, "from_user", None)
        if sender is None:
            return False
        if "me" in wanted and sender.id == client.me_id:
            return True
        return sender.id in wanted or (sender.username or "").lower() in wanted

    return create(func, "user", users=wanted)


def chat(chats: Union[int, str, Iterable[Union[int, str]]]) -> Filter:
    """Updates in these chat ids or usernames."""
    wanted = _ids(chats)

    def func(flt, client, update) -> bool:
        m = _msg(update)
        if m is None or m.chat is None:
            return False
        return m.chat.id in wanted or (m.chat.username or "").lower() in wanted

    return create(func, "chat", chats=wanted)
