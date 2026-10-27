from __future__ import annotations

import html
from typing import Optional

from wirelet import raw, utils
from .object import Object


class User(Object):
    """A Telegram user or bot."""

    def __init__(self, *, id: int, is_bot: bool = False, first_name: Optional[str] = None,
                 last_name: Optional[str] = None, username: Optional[str] = None,
                 phone_number: Optional[str] = None, language_code: Optional[str] = None,
                 is_self: bool = False, is_contact: bool = False, is_verified: bool = False,
                 is_premium: bool = False, is_deleted: bool = False, is_scam: bool = False,
                 is_fake: bool = False, raw=None) -> None:
        self.id = id
        self.is_bot = is_bot
        self.first_name = first_name
        self.last_name = last_name
        self.username = username
        self.phone_number = phone_number
        self.language_code = language_code
        self.is_self = is_self
        self.is_contact = is_contact
        self.is_verified = is_verified
        self.is_premium = is_premium
        self.is_deleted = is_deleted
        self.is_scam = is_scam
        self.is_fake = is_fake
        self.raw = raw

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p) or "Deleted Account"

    @property
    def mention(self) -> str:
        """An HTML link to this user, for ``parse_mode="html"``."""
        return f'<a href="tg://user?id={self.id}">{html.escape(self.full_name)}</a>'

    @staticmethod
    def _parse(client, user) -> Optional["User"]:
        if user is None:
            return None
        if isinstance(user, raw.types.UserEmpty):
            return User(id=user.id, raw=user)._bind(client)
        return User(
            id=user.id,
            is_bot=bool(user.bot),
            first_name=user.first_name,
            last_name=user.last_name,
            username=utils._username(user),
            phone_number=user.phone,
            language_code=user.lang_code,
            is_self=bool(user.is_self),
            is_contact=bool(user.contact),
            is_verified=bool(user.verified),
            is_premium=bool(user.premium),
            is_deleted=bool(user.deleted),
            is_scam=bool(user.scam),
            is_fake=bool(user.fake),
            raw=user,
        )._bind(client)


class Chat(Object):
    """A private chat, group, supergroup or channel.

    ``type`` is one of ``"private"``, ``"bot"``, ``"group"``, ``"supergroup"``
    or ``"channel"``.
    """

    def __init__(self, *, id: int, type: str, title: Optional[str] = None,
                 username: Optional[str] = None, first_name: Optional[str] = None,
                 last_name: Optional[str] = None, is_verified: bool = False,
                 is_forum: bool = False, members_count: Optional[int] = None, raw=None) -> None:
        self.id = id
        self.type = type
        self.title = title
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.is_verified = is_verified
        self.is_forum = is_forum
        self.members_count = members_count
        self.raw = raw

    @property
    def name(self) -> str:
        return self.title or " ".join(p for p in (self.first_name, self.last_name) if p)

    @staticmethod
    def _parse_user(client, user) -> "Chat":
        return Chat(
            id=user.id,
            type="bot" if getattr(user, "bot", False) else "private",
            username=utils._username(user),
            first_name=getattr(user, "first_name", None),
            last_name=getattr(user, "last_name", None),
            is_verified=bool(getattr(user, "verified", False)),
            raw=user,
        )._bind(client)

    @staticmethod
    def _parse_chat(client, chat) -> "Chat":
        if isinstance(chat, (raw.types.Channel, raw.types.ChannelForbidden)):
            return Chat(
                id=utils.CHANNEL_SHIFT - chat.id,
                type="supergroup" if chat.megagroup else "channel",
                title=chat.title,
                username=utils._username(chat),
                is_verified=bool(getattr(chat, "verified", False)),
                is_forum=bool(getattr(chat, "forum", False)),
                members_count=getattr(chat, "participants_count", None),
                raw=chat,
            )._bind(client)
        return Chat(
            id=-chat.id,
            type="group",
            title=getattr(chat, "title", None),
            members_count=getattr(chat, "participants_count", None),
            raw=chat,
        )._bind(client)

    @staticmethod
    def _parse_peer(client, peer, users: dict, chats: dict) -> Optional["Chat"]:
        if peer is None:
            return None
        if isinstance(peer, raw.types.PeerUser):
            user = users.get(peer.user_id)
            return Chat._parse_user(client, user) if user else Chat(id=peer.user_id, type="private")._bind(client)
        if isinstance(peer, raw.types.PeerChat):
            chat = chats.get(peer.chat_id)
            return Chat._parse_chat(client, chat) if chat else Chat(id=-peer.chat_id, type="group")._bind(client)
        if isinstance(peer, raw.types.PeerChannel):
            chat = chats.get(peer.channel_id)
            if chat:
                return Chat._parse_chat(client, chat)
            return Chat(id=utils.CHANNEL_SHIFT - peer.channel_id, type="channel")._bind(client)
        return None
