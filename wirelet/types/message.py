from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Union

from wirelet import raw, utils
from wirelet.parser import unparse_html
from .keyboards import InlineKeyboardMarkup
from .media import Document, Photo, parse_document
from .object import Object
from .user import Chat, User


class Message(Object):
    """A message in any chat.

    Text messages have ``text``; media messages have the matching attribute
    (``photo``, ``video``, ``document``...) and ``caption``. ``media`` names
    which one is set. Service messages (joins, pins...) have ``service`` set to
    the raw action.
    """

    id: int
    chat: Optional[Chat]

    def __init__(self, *, id: int, chat: Optional[Chat] = None, from_user: Optional[User] = None,
                 sender_chat: Optional[Chat] = None, date: Optional[datetime] = None,
                 text: Optional[str] = None, entities: Optional[List] = None,
                 caption: Optional[str] = None, caption_entities: Optional[List] = None,
                 media: Optional[str] = None, photo: Optional[Photo] = None,
                 video=None, video_note=None, animation=None, audio=None, voice=None,
                 sticker=None, document: Optional[Document] = None,
                 reply_to_message_id: Optional[int] = None, message_thread_id: Optional[int] = None,
                 forward_date: Optional[datetime] = None, edit_date: Optional[datetime] = None,
                 outgoing: bool = False, mentioned: bool = False, pinned: bool = False,
                 views: Optional[int] = None, media_group_id: Optional[int] = None,
                 via_bot_id: Optional[int] = None, reply_markup=None, service=None,
                 empty: bool = False, raw=None) -> None:
        self.id = id
        self.chat = chat
        self.from_user = from_user
        self.sender_chat = sender_chat
        self.date = date
        self.text = text
        self.entities = entities
        self.caption = caption
        self.caption_entities = caption_entities
        self.media = media
        self.photo = photo
        self.video = video
        self.video_note = video_note
        self.animation = animation
        self.audio = audio
        self.voice = voice
        self.sticker = sticker
        self.document = document
        self.reply_to_message_id = reply_to_message_id
        self.message_thread_id = message_thread_id
        self.forward_date = forward_date
        self.edit_date = edit_date
        self.outgoing = outgoing
        self.mentioned = mentioned
        self.pinned = pinned
        self.views = views
        self.media_group_id = media_group_id
        self.via_bot_id = via_bot_id
        self.reply_markup = reply_markup
        self.service = service
        self.empty = empty
        self.raw = raw
        # Set by the command filter.
        self.command: Optional[List[str]] = None
        # Set by the regex filter.
        self.matches: Optional[List[Any]] = None

    # -- convenience -------------------------------------------------------- #

    @property
    def content(self) -> Optional[str]:
        """``text`` or ``caption``, whichever this message has."""
        return self.text if self.text is not None else self.caption

    @property
    def html(self) -> Optional[str]:
        """The text or caption with its formatting as HTML."""
        if self.text is not None:
            return unparse_html(self.text, self.entities)
        if self.caption is not None:
            return unparse_html(self.caption, self.caption_entities)
        return None

    @property
    def link(self) -> Optional[str]:
        if self.chat is None or self.chat.type not in ("channel", "supergroup"):
            return None
        if self.chat.username:
            return f"https://t.me/{self.chat.username}/{self.id}"
        return f"https://t.me/c/{utils.to_raw_id(self.chat.id)}/{self.id}"

    @property
    def file(self):
        """Whichever media object this message carries, or None."""
        return getattr(self, self.media) if self.media else None

    # -- actions ----------------------------------------------------------- #

    async def reply(self, text: str, *, quote: bool = True, **kwargs) -> "Message":
        """Send a text message to the same chat, replying to this one."""
        if quote:
            kwargs.setdefault("reply_to_message_id", self.id)
        if self.message_thread_id:
            kwargs.setdefault("message_thread_id", self.message_thread_id)
        return await self._client.send_message(self.chat.id, text, **kwargs)

    reply_text = reply

    async def _reply_media(self, method: str, media, quote: bool, kwargs) -> "Message":
        if quote:
            kwargs.setdefault("reply_to_message_id", self.id)
        return await getattr(self._client, method)(self.chat.id, media, **kwargs)

    async def reply_photo(self, photo, *, quote: bool = True, **kwargs) -> "Message":
        return await self._reply_media("send_photo", photo, quote, kwargs)

    async def reply_document(self, document, *, quote: bool = True, **kwargs) -> "Message":
        return await self._reply_media("send_document", document, quote, kwargs)

    async def reply_video(self, video, *, quote: bool = True, **kwargs) -> "Message":
        return await self._reply_media("send_video", video, quote, kwargs)

    async def reply_audio(self, audio, *, quote: bool = True, **kwargs) -> "Message":
        return await self._reply_media("send_audio", audio, quote, kwargs)

    async def reply_voice(self, voice, *, quote: bool = True, **kwargs) -> "Message":
        return await self._reply_media("send_voice", voice, quote, kwargs)

    async def edit_text(self, text: str, **kwargs) -> "Message":
        return await self._client.edit_message_text(self.chat.id, self.id, text, **kwargs)

    edit = edit_text

    async def edit_reply_markup(self, reply_markup=None) -> "Message":
        return await self._client.edit_message_reply_markup(self.chat.id, self.id, reply_markup)

    async def delete(self, revoke: bool = True) -> int:
        return await self._client.delete_messages(self.chat.id, [self.id], revoke=revoke)

    async def forward(self, chat_id: Union[int, str], **kwargs) -> "Message":
        result = await self._client.forward_messages(chat_id, self.chat.id, [self.id], **kwargs)
        return result[0]

    async def copy(self, chat_id: Union[int, str], **kwargs) -> "Message":
        return await self._client.copy_message(chat_id, self.chat.id, self.id, **kwargs)

    async def pin(self, disable_notification: bool = False) -> None:
        await self._client.pin_chat_message(self.chat.id, self.id, disable_notification=disable_notification)

    async def download(self, file_name: Optional[str] = None, **kwargs):
        return await self._client.download_media(self, file_name, **kwargs)

    async def get_reply_message(self) -> Optional["Message"]:
        if not self.reply_to_message_id:
            return None
        messages = await self._client.get_messages(self.chat.id, [self.reply_to_message_id])
        return messages[0] if messages and not messages[0].empty else None

    # -- parsing ---------------------------------------------------------- #

    @staticmethod
    def _parse(client, message, users: dict, chats: dict) -> "Message":
        if isinstance(message, raw.types.MessageEmpty):
            return Message(id=message.id, empty=True, raw=message)._bind(client)

        chat = Chat._parse_peer(client, message.peer_id, users, chats)
        from_user, sender_chat = None, None

        from_id = message.from_id
        if from_id is None and isinstance(message.peer_id, raw.types.PeerUser):
            # In a private chat the other side is the sender unless we sent it.
            if not message.out:
                from_id = message.peer_id
            elif client.me_id:
                from_id = raw.types.PeerUser(user_id=client.me_id)

        if isinstance(from_id, raw.types.PeerUser):
            user = users.get(from_id.user_id)
            from_user = User._parse(client, user) if user else User(id=from_id.user_id)._bind(client)
        elif from_id is not None:
            sender_chat = Chat._parse_peer(client, from_id, users, chats)
        elif chat is not None and chat.type == "channel":
            sender_chat = chat

        reply_to_id = thread_id = None
        reply = message.reply_to
        if isinstance(reply, raw.types.MessageReplyHeader):
            reply_to_id = reply.reply_to_msg_id
            if reply.forum_topic:
                thread_id = reply.reply_to_top_id or reply.reply_to_msg_id

        common = dict(
            id=message.id,
            chat=chat,
            from_user=from_user,
            sender_chat=sender_chat,
            date=utils.timestamp_to_datetime(message.date),
            reply_to_message_id=reply_to_id,
            message_thread_id=thread_id,
            outgoing=bool(message.out),
            mentioned=bool(message.mentioned),
            raw=message,
        )

        if isinstance(message, raw.types.MessageService):
            return Message(service=message.action, **common)._bind(client)

        kind, media_obj = None, None
        media = message.media
        if isinstance(media, raw.types.MessageMediaPhoto):
            media_obj = Photo._parse(client, media.photo)
            kind = "photo" if media_obj else None
        elif isinstance(media, raw.types.MessageMediaDocument):
            media_obj = parse_document(client, media.document, media)
            if media_obj is not None:
                kind = {
                    "Video": "video", "VideoNote": "video_note", "Animation": "animation",
                    "Audio": "audio", "Voice": "voice", "Sticker": "sticker",
                }.get(type(media_obj).__name__, "document")
        elif media is not None and not isinstance(media, (raw.types.MessageMediaEmpty,
                                                          raw.types.MessageMediaWebPage)):
            # Polls, locations, contacts, dice...: available as message.raw.media
            kind = type(media).__name__.removeprefix("MessageMedia").lower()

        has_file = kind is not None and media_obj is not None
        text_fields = (
            dict(caption=message.message or None, caption_entities=message.entities)
            if has_file else dict(text=message.message, entities=message.entities)
        )

        extra = {kind: media_obj} if has_file else {}
        return Message(
            **common,
            **text_fields,
            **extra,
            media=kind,
            forward_date=utils.timestamp_to_datetime(message.fwd_from.date) if message.fwd_from else None,
            edit_date=utils.timestamp_to_datetime(message.edit_date),
            pinned=bool(message.pinned),
            views=message.views,
            media_group_id=message.grouped_id,
            via_bot_id=message.via_bot_id,
            reply_markup=InlineKeyboardMarkup._parse(message.reply_markup) or message.reply_markup,
        )._bind(client)


class CallbackQuery(Object):
    """Someone pressed an inline button with ``callback_data``."""

    def __init__(self, *, id: int, from_user: User, chat_instance: int,
                 data: Union[str, bytes, None], message: Optional[Message] = None,
                 inline_message_id=None, game_short_name: Optional[str] = None, raw=None) -> None:
        self.id = id
        self.from_user = from_user
        self.chat_instance = chat_instance
        self.data = data
        self.message = message
        self.inline_message_id = inline_message_id
        self.game_short_name = game_short_name
        self.raw = raw
        self.matches: Optional[List[Any]] = None

    async def answer(self, text: Optional[str] = None, show_alert: bool = False,
                     url: Optional[str] = None, cache_time: int = 0) -> bool:
        """Stop the button's loading spinner, optionally with a toast or alert."""
        return await self._client.answer_callback_query(
            self.id, text=text, show_alert=show_alert, url=url, cache_time=cache_time)

    async def edit_message_text(self, text: str, **kwargs) -> Optional[Message]:
        if self.inline_message_id is not None:
            await self._client.edit_inline_text(self.inline_message_id, text, **kwargs)
            return None
        return await self._client.edit_message_text(self.message.chat.id, self.message.id, text, **kwargs)

    async def edit_message_reply_markup(self, reply_markup=None) -> Optional[Message]:
        return await self._client.edit_message_reply_markup(
            self.message.chat.id, self.message.id, reply_markup)


class InlineQuery(Object):
    """Someone typed ``@yourbot query`` in a chat."""

    def __init__(self, *, id: int, from_user: User, query: str, offset: str,
                 chat_type: Optional[str] = None, raw=None) -> None:
        self.id = id
        self.from_user = from_user
        self.query = query
        self.offset = offset
        self.chat_type = chat_type
        self.raw = raw
        self.matches: Optional[List[Any]] = None

    async def answer(self, results: List["InlineQueryResultArticle"], cache_time: int = 300,
                     is_personal: bool = False, next_offset: str = "") -> bool:
        return await self._client.answer_inline_query(
            self.id, results, cache_time=cache_time, is_personal=is_personal, next_offset=next_offset)


class InlineQueryResultArticle(Object):
    """A text result for an inline query."""

    def __init__(self, title: str, text: str, *, id: Optional[str] = None,
                 description: Optional[str] = None, url: Optional[str] = None,
                 thumb_url: Optional[str] = None, parse_mode: Optional[str] = "default",
                 reply_markup: Optional[InlineKeyboardMarkup] = None,
                 disable_web_page_preview: bool = False) -> None:
        import uuid

        self.id = id or uuid.uuid4().hex
        self.title = title
        self.text = text
        self.description = description
        self.url = url
        self.thumb_url = thumb_url
        self.parse_mode = parse_mode
        self.reply_markup = reply_markup
        self.disable_web_page_preview = disable_web_page_preview

    async def write(self, client):
        text, entities = await client._parse_text(self.text, self.parse_mode, None)
        thumb = None
        if self.thumb_url:
            thumb = raw.types.InputWebDocument(url=self.thumb_url, size=0, mime_type="image/jpeg", attributes=[])
        return raw.types.InputBotInlineResult(
            id=self.id,
            type="article",
            title=self.title,
            description=self.description,
            url=self.url,
            thumb=thumb,
            send_message=raw.types.InputBotInlineMessageText(
                message=text,
                entities=entities or None,
                no_webpage=self.disable_web_page_preview or None,
                reply_markup=await self.reply_markup.write(client) if self.reply_markup else None,
            ),
        )
