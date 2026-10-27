"""Messages."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, List, Optional, Union

from wirelet import schema, utils
from wirelet.parser import unparse_html
from .base import Model
from .media import Media, media_from
from .people import Chat, User, chat_from_peer, user_from

ChatRef = Union[int, str]


class Message(Model):
    """One message.

    ``text`` holds the text or, for media, the caption; ``media`` the attached
    media (with ``media.kind``). Service messages (joins, pins...) have the raw
    ``action`` instead. Commands caught by ``wire.command`` set ``cmd``,
    ``args`` and ``rest``; a ``pattern=`` match sets ``match``.
    """

    def __init__(self, *, id: int, chat: Optional[Chat] = None, author: Optional[User] = None,
                 author_chat: Optional[Chat] = None, text: str = "", entities: Optional[List] = None,
                 media: Optional[Media] = None, sent_at: Optional[datetime] = None,
                 edited_at: Optional[datetime] = None, forwarded_at: Optional[datetime] = None,
                 reply_to: Optional[int] = None, topic_id: Optional[int] = None,
                 from_me: bool = False, mentioned: bool = False, pinned: bool = False,
                 views: Optional[int] = None, album_id: Optional[int] = None,
                 via_bot_id: Optional[int] = None, buttons=None, action=None,
                 exists: bool = True, raw=None) -> None:
        self.id = id
        self.chat = chat
        self.author = author
        self.author_chat = author_chat
        self.text = text
        self.entities = entities or []
        self.media = media
        self.sent_at = sent_at
        self.edited_at = edited_at
        self.forwarded_at = forwarded_at
        self.reply_to = reply_to
        self.topic_id = topic_id
        self.from_me = from_me
        self.mentioned = mentioned
        self.pinned = pinned
        self.views = views
        self.album_id = album_id
        self.via_bot_id = via_bot_id
        self.buttons = buttons
        self.action = action
        self.exists = exists
        self.raw = raw
        self.cmd: Optional[str] = None
        self.args: List[str] = []
        self.rest: str = ""
        self.match: Optional[re.Match] = None

    # -- reading -------------------------------------------------------- #

    @property
    def html(self) -> str:
        """The text with its formatting, as HTML."""
        return unparse_html(self.text, self.entities)

    @property
    def link(self) -> Optional[str]:
        """A t.me link, for messages in channels and supergroups."""
        if self.chat is None or self.chat.kind not in ("channel", "supergroup"):
            return None
        if self.chat.handle:
            return f"https://t.me/{self.chat.handle}/{self.id}"
        return f"https://t.me/c/{utils.to_raw_id(self.chat.id)}/{self.id}"

    @property
    def kind(self) -> str:
        """``"text"``, ``"action"`` or the media kind (``"photo"``, ``"voice"``...)."""
        if self.action is not None:
            return "action"
        return self.media.kind if self.media is not None else "text"

    async def replied(self) -> Optional["Message"]:
        """The message this one replies to, fetched from Telegram."""
        if not self.reply_to:
            return None
        found = await self.wire.fetch(self.chat.id, self.reply_to)
        return found if found is not None and found.exists else None

    # -- acting --------------------------------------------------------- #

    async def say(self, text: str = "", **options) -> "Message":
        """Send a message to the same chat (and topic). Takes :meth:`Wire.send`'s options."""
        if self.topic_id:
            options.setdefault("topic", self.topic_id)
        return await self.wire.send(self.chat.id, text, **options)

    async def quote(self, text: str = "", **options) -> "Message":
        """Like :meth:`say`, as a reply to this message."""
        options.setdefault("reply_to", self.id)
        return await self.say(text, **options)

    async def ask(self, text: str, *, timeout: float = 120, **options) -> "Message":
        """Say ``text``, then wait for this author's next message in this chat and return it.

        Raises ``asyncio.TimeoutError`` if they don't answer in time.
        """
        await self.say(text, **options)
        author = self.author.id if self.author else None
        return await self.wire.wait_message(chat=self.chat.id, by=author, timeout=timeout)

    async def rewrite(self, text: str, **options) -> "Message":
        """Edit this message's text (or caption). Takes ``buttons``, ``format``, ``preview``."""
        return await self.wire.rewrite(self.chat.id, self.id, text, **options)

    async def rebutton(self, buttons=None) -> "Message":
        """Replace the buttons under this message (None removes them)."""
        return await self.wire.rebutton(self.chat.id, self.id, buttons)

    async def erase(self, *, everyone: bool = True) -> int:
        return await self.wire.erase(self.chat.id, self.id, everyone=everyone)

    async def relay(self, to: ChatRef, *, anonymous: bool = False, silent: bool = False) -> "Message":
        """Forward this message to ``to``; ``anonymous`` hides where it came from."""
        sent = await self.wire.relay(to, self.chat.id, [self.id], anonymous=anonymous, silent=silent)
        return sent[0]

    async def pin(self, *, silent: bool = False) -> None:
        await self.wire.pin(self.chat.id, self.id, silent=silent)

    async def unpin(self) -> None:
        await self.wire.unpin(self.chat.id, self.id)

    async def save(self, to=None, *, memory: bool = False, progress=None):
        """Download this message's file. Returns the path (or BytesIO with ``memory``)."""
        return await self.wire.download(self, to, memory=memory, progress=progress)

    def typing(self, action: str = "typing"):
        """``async with msg.typing():`` shows an activity in this chat while you work."""
        return self.wire.typing(self.chat.id, action)


def message_from(wire, message, users: Dict, chats: Dict) -> Message:
    o = schema.objects
    if isinstance(message, o.MessageEmpty):
        return Message(id=message.id, exists=False, raw=message)._attach(wire)

    chat = chat_from_peer(wire, message.peer_id, users, chats)

    sender = message.from_id
    if sender is None and isinstance(message.peer_id, o.PeerUser):
        # In a private chat the other side wrote it, unless we did.
        if not message.out:
            sender = message.peer_id
        elif wire.me_id:
            sender = o.PeerUser(user_id=wire.me_id)

    author, author_chat = None, None
    if isinstance(sender, o.PeerUser):
        user = users.get(sender.user_id)
        author = user_from(wire, user) if user else User(id=sender.user_id)._attach(wire)
    elif sender is not None:
        author_chat = chat_from_peer(wire, sender, users, chats)
    elif chat is not None and chat.kind == "channel":
        author_chat = chat

    reply_to = topic_id = None
    header = message.reply_to
    if isinstance(header, o.MessageReplyHeader):
        reply_to = header.reply_to_msg_id
        if header.forum_topic:
            topic_id = header.reply_to_top_id or header.reply_to_msg_id

    common = dict(
        id=message.id, chat=chat, author=author, author_chat=author_chat,
        sent_at=utils.timestamp_to_datetime(message.date), reply_to=reply_to, topic_id=topic_id,
        from_me=bool(message.out), mentioned=bool(message.mentioned), raw=message,
    )

    if isinstance(message, o.MessageService):
        return Message(action=message.action, **common)._attach(wire)

    from wirelet.ui import grid_from

    return Message(
        **common,
        text=message.message or "",
        entities=message.entities,
        media=media_from(wire, message.media),
        edited_at=utils.timestamp_to_datetime(message.edit_date),
        forwarded_at=utils.timestamp_to_datetime(message.fwd_from.date) if message.fwd_from else None,
        pinned=bool(message.pinned),
        views=message.views,
        album_id=message.grouped_id,
        via_bot_id=message.via_bot_id,
        buttons=grid_from(message.reply_markup) or message.reply_markup,
    )._attach(wire)
