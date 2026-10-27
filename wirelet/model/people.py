"""Users and chats."""

from __future__ import annotations

import html
from typing import Dict, Optional

from wirelet import schema, utils
from .base import Model


class User(Model):
    """A person or a bot.

    ``name`` is the display name; ``handle`` the username without ``@``;
    ``tag`` an HTML link that mentions them.
    """

    def __init__(self, *, id: int, first: Optional[str] = None, last: Optional[str] = None,
                 handle: Optional[str] = None, phone: Optional[str] = None, lang: Optional[str] = None,
                 bot: bool = False, premium: bool = False, verified: bool = False,
                 deleted: bool = False, scam: bool = False, fake: bool = False,
                 contact: bool = False, is_me: bool = False, raw=None) -> None:
        self.id = id
        self.first = first
        self.last = last
        self.handle = handle
        self.phone = phone
        self.lang = lang
        self.bot = bot
        self.premium = premium
        self.verified = verified
        self.deleted = deleted
        self.scam = scam
        self.fake = fake
        self.contact = contact
        self.is_me = is_me
        self.raw = raw

    @property
    def name(self) -> str:
        return " ".join(p for p in (self.first, self.last) if p) or ("Deleted" if self.deleted else str(self.id))

    @property
    def tag(self) -> str:
        return f'<a href="tg://user?id={self.id}">{html.escape(self.name)}</a>'

    @property
    def at(self) -> Optional[str]:
        """``@handle``, or None without a username."""
        return f"@{self.handle}" if self.handle else None


class Chat(Model):
    """Where messages live. ``kind`` is ``"dm"``, ``"bot"`` (a chat with a bot),
    ``"group"``, ``"supergroup"`` or ``"channel"``."""

    def __init__(self, *, id: int, kind: str, title: Optional[str] = None, handle: Optional[str] = None,
                 first: Optional[str] = None, last: Optional[str] = None, verified: bool = False,
                 forum: bool = False, members: Optional[int] = None, raw=None) -> None:
        self.id = id
        self.kind = kind
        self.title = title
        self.handle = handle
        self.first = first
        self.last = last
        self.verified = verified
        self.forum = forum
        self.members = members
        self.raw = raw

    @property
    def name(self) -> str:
        return self.title or " ".join(p for p in (self.first, self.last) if p) or str(self.id)

    @property
    def is_private(self) -> bool:
        return self.kind in ("dm", "bot")


def user_from(wire, user) -> Optional[User]:
    if user is None:
        return None
    if not isinstance(user, schema.objects.User):
        return User(id=user.id, raw=user)._attach(wire)
    return User(
        id=user.id, first=user.first_name, last=user.last_name, handle=utils.handle_of(user),
        phone=user.phone, lang=user.lang_code, bot=bool(user.bot), premium=bool(user.premium),
        verified=bool(user.verified), deleted=bool(user.deleted), scam=bool(user.scam),
        fake=bool(user.fake), contact=bool(user.contact), is_me=bool(user.is_self), raw=user,
    )._attach(wire)


def chat_from_user(wire, user) -> Chat:
    return Chat(
        id=user.id, kind="bot" if getattr(user, "bot", False) else "dm",
        handle=utils.handle_of(user), first=getattr(user, "first_name", None),
        last=getattr(user, "last_name", None), verified=bool(getattr(user, "verified", False)), raw=user,
    )._attach(wire)


def chat_from_chat(wire, chat) -> Chat:
    o = schema.objects
    if isinstance(chat, (o.Channel, o.ChannelForbidden)):
        return Chat(
            id=utils.CHANNEL_SHIFT - chat.id, kind="supergroup" if chat.megagroup else "channel",
            title=chat.title, handle=utils.handle_of(chat),
            verified=bool(getattr(chat, "verified", False)), forum=bool(getattr(chat, "forum", False)),
            members=getattr(chat, "participants_count", None), raw=chat,
        )._attach(wire)
    return Chat(id=-chat.id, kind="group", title=getattr(chat, "title", None),
                members=getattr(chat, "participants_count", None), raw=chat)._attach(wire)


def chat_from_peer(wire, peer, users: Dict, chats: Dict) -> Optional[Chat]:
    o = schema.objects
    if isinstance(peer, o.PeerUser):
        user = users.get(peer.user_id)
        return chat_from_user(wire, user) if user else Chat(id=peer.user_id, kind="dm")._attach(wire)
    if isinstance(peer, o.PeerChat):
        chat = chats.get(peer.chat_id)
        return chat_from_chat(wire, chat) if chat else Chat(id=-peer.chat_id, kind="group")._attach(wire)
    if isinstance(peer, o.PeerChannel):
        chat = chats.get(peer.channel_id)
        if chat:
            return chat_from_chat(wire, chat)
        return Chat(id=utils.CHANNEL_SHIFT - peer.channel_id, kind="channel")._attach(wire)
    return None
