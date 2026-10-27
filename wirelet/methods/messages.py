from __future__ import annotations

from datetime import datetime
from typing import AsyncGenerator, List, Optional, Sequence, Union

from wirelet import errors, parser, raw, utils
from wirelet.types import Message
from wirelet.types.keyboards import ReplyMarkup

ChatId = Union[int, str]
_DEFAULT = "default"

CHAT_ACTIONS = {
    "typing": raw.types.SendMessageTypingAction,
    "cancel": raw.types.SendMessageCancelAction,
    "record_video": raw.types.SendMessageRecordVideoAction,
    "record_voice": raw.types.SendMessageRecordAudioAction,
    "record_video_note": raw.types.SendMessageRecordRoundAction,
    "choose_sticker": raw.types.SendMessageChooseStickerAction,
    "find_location": raw.types.SendMessageGeoLocationAction,
    "choose_contact": raw.types.SendMessageChooseContactAction,
    "playing": raw.types.SendMessageGamePlayAction,
}
UPLOAD_ACTIONS = {
    "upload_photo": raw.types.SendMessageUploadPhotoAction,
    "upload_video": raw.types.SendMessageUploadVideoAction,
    "upload_voice": raw.types.SendMessageUploadAudioAction,
    "upload_document": raw.types.SendMessageUploadDocumentAction,
    "upload_video_note": raw.types.SendMessageUploadRoundAction,
}


class Messages:
    # -- helpers ----------------------------------------------------------- #

    async def _parse_text(self, text: str, parse_mode: Optional[str], entities):
        """Return ``(text, raw entities)`` for sending."""
        if entities is not None:
            return text, entities
        mode = self.parse_mode if parse_mode == _DEFAULT else parse_mode
        plain, parsed, mentions = parser.parse(text or "", mode)
        for offset, length, user_id in mentions:
            try:
                user = utils.to_input_user(await self.resolve_peer(user_id))
            except (errors.RPCError, ValueError):
                continue
            parsed.append(raw.types.InputMessageEntityMentionName(offset=offset, length=length, user_id=user))
        parsed.sort(key=lambda e: e.offset)
        return plain, parsed or None

    async def _markup(self, reply_markup: Optional[ReplyMarkup]):
        return await reply_markup.write(self) if reply_markup is not None else None

    @staticmethod
    def _reply_to(reply_to_message_id: Optional[int], message_thread_id: Optional[int]):
        if reply_to_message_id is None and message_thread_id is None:
            return None
        return raw.types.InputReplyToMessage(
            reply_to_msg_id=reply_to_message_id or message_thread_id,
            top_msg_id=message_thread_id,
        )

    def _messages_from(self, result) -> List[Message]:
        """Messages contained in an Updates or messages.Messages result, in order."""
        t = raw.types
        users = {u.id: u for u in getattr(result, "users", [])}
        chats = {c.id: c for c in getattr(result, "chats", [])}

        if isinstance(result, (t.Updates, t.UpdatesCombined)):
            raws = [
                u.message for u in result.updates
                if isinstance(u, (t.UpdateNewMessage, t.UpdateNewChannelMessage, t.UpdateNewScheduledMessage,
                                  t.UpdateEditMessage, t.UpdateEditChannelMessage, t.UpdateBotNewBusinessMessage))
            ]
        elif isinstance(result, t.UpdateShort) and hasattr(result.update, "message"):
            raws = [result.update.message]
        elif hasattr(result, "messages"):
            raws = result.messages
        else:
            raws = []

        return [Message._parse(self, m, users, chats) for m in raws]

    async def _sent_message(self, result, peer, random_id: int) -> Message:
        if isinstance(result, raw.types.UpdateShortSentMessage):
            # Sent in a private chat: Telegram returns only the new id.
            messages = await self.get_messages(utils.get_peer_id(_peer_of(peer, self.me_id)), [result.id])
            return messages[0]
        await self._remember_peers(result)
        messages = self._messages_from(result)
        if not messages:
            raise errors.named("MessageNotReturned")("Telegram did not return the sent message", 500)
        return messages[0]

    # -- sending ---------------------------------------------------------- #

    async def send_message(
        self,
        chat_id: ChatId,
        text: str,
        *,
        parse_mode: Optional[str] = _DEFAULT,
        entities: Optional[list] = None,
        reply_to_message_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
        reply_markup: Optional[ReplyMarkup] = None,
        disable_web_page_preview: bool = False,
        disable_notification: bool = False,
        protect_content: bool = False,
        schedule_date: Optional[datetime] = None,
    ) -> Message:
        """Send a text message. ``chat_id`` can be an id, ``@username`` or ``"me"``."""
        peer = await self.resolve_peer(chat_id)
        message, entities = await self._parse_text(text, parse_mode, entities)
        random_id = utils.random_id()

        result = await self.invoke(raw.functions.messages.SendMessage(
            peer=peer,
            message=message,
            random_id=random_id,
            entities=entities,
            reply_to=self._reply_to(reply_to_message_id, message_thread_id),
            reply_markup=await self._markup(reply_markup),
            no_webpage=disable_web_page_preview or None,
            silent=disable_notification or None,
            noforwards=protect_content or None,
            schedule_date=utils.datetime_to_timestamp(schedule_date),
        ))
        return await self._sent_message(result, peer, random_id)

    async def edit_message_text(
        self,
        chat_id: ChatId,
        message_id: int,
        text: str,
        *,
        parse_mode: Optional[str] = _DEFAULT,
        entities: Optional[list] = None,
        reply_markup: Optional[ReplyMarkup] = None,
        disable_web_page_preview: bool = False,
    ) -> Message:
        message, entities = await self._parse_text(text, parse_mode, entities)
        result = await self.invoke(raw.functions.messages.EditMessage(
            peer=await self.resolve_peer(chat_id),
            id=message_id,
            message=message,
            entities=entities,
            reply_markup=await self._markup(reply_markup),
            no_webpage=disable_web_page_preview or None,
        ))
        return self._messages_from(result)[0]

    async def edit_message_reply_markup(self, chat_id: ChatId, message_id: int,
                                        reply_markup: Optional[ReplyMarkup] = None) -> Message:
        markup = await self._markup(reply_markup) or raw.types.ReplyInlineMarkup(rows=[])
        result = await self.invoke(raw.functions.messages.EditMessage(
            peer=await self.resolve_peer(chat_id), id=message_id, reply_markup=markup,
        ))
        return self._messages_from(result)[0]

    async def edit_inline_text(self, inline_message_id, text: str, *, parse_mode: Optional[str] = _DEFAULT,
                               entities: Optional[list] = None, reply_markup: Optional[ReplyMarkup] = None,
                               disable_web_page_preview: bool = False) -> bool:
        """Edit a message that was sent through your bot in inline mode."""
        message, entities = await self._parse_text(text, parse_mode, entities)
        session = await self._session_for(inline_message_id.dc_id, media=False)
        return await session.invoke(raw.functions.messages.EditInlineBotMessage(
            id=inline_message_id, message=message, entities=entities,
            reply_markup=await self._markup(reply_markup),
            no_webpage=disable_web_page_preview or None,
        ))

    async def delete_messages(self, chat_id: ChatId, message_ids: Union[int, Sequence[int]],
                              revoke: bool = True) -> int:
        """Delete messages; returns how many were deleted."""
        ids = [message_ids] if isinstance(message_ids, int) else list(message_ids)
        peer = await self.resolve_peer(chat_id)
        if isinstance(peer, raw.types.InputPeerChannel):
            result = await self.invoke(raw.functions.channels.DeleteMessages(
                channel=utils.to_input_channel(peer), id=ids))
        else:
            result = await self.invoke(raw.functions.messages.DeleteMessages(id=ids, revoke=revoke or None))
        return result.pts_count

    async def forward_messages(self, chat_id: ChatId, from_chat_id: ChatId,
                               message_ids: Union[int, Sequence[int]], *, drop_author: bool = False,
                               disable_notification: bool = False, message_thread_id: Optional[int] = None,
                               protect_content: bool = False) -> List[Message]:
        ids = [message_ids] if isinstance(message_ids, int) else list(message_ids)
        result = await self.invoke(raw.functions.messages.ForwardMessages(
            from_peer=await self.resolve_peer(from_chat_id),
            to_peer=await self.resolve_peer(chat_id),
            id=ids,
            random_id=[utils.random_id() for _ in ids],
            drop_author=drop_author or None,
            silent=disable_notification or None,
            noforwards=protect_content or None,
            top_msg_id=message_thread_id,
        ))
        await self._remember_peers(result)
        return self._messages_from(result)

    async def copy_message(self, chat_id: ChatId, from_chat_id: ChatId, message_id: int, **kwargs) -> Message:
        """Send a copy without the "Forwarded from" header."""
        messages = await self.forward_messages(chat_id, from_chat_id, [message_id], drop_author=True, **kwargs)
        return messages[0]

    # -- reading ---------------------------------------------------------- #

    async def get_messages(self, chat_id: ChatId, message_ids: Union[int, Sequence[int]]) -> List[Message]:
        """Fetch messages by id. Missing ones come back with ``empty=True``."""
        ids = [message_ids] if isinstance(message_ids, int) else list(message_ids)
        peer = await self.resolve_peer(chat_id)
        inputs = [raw.types.InputMessageID(id=i) for i in ids]
        if isinstance(peer, raw.types.InputPeerChannel):
            result = await self.invoke(raw.functions.channels.GetMessages(
                channel=utils.to_input_channel(peer), id=inputs))
        else:
            result = await self.invoke(raw.functions.messages.GetMessages(id=inputs))
        return self._messages_from(result)

    async def get_chat_history(self, chat_id: ChatId, limit: int = 0,
                               offset_id: int = 0) -> AsyncGenerator[Message, None]:
        """Iterate over a chat's messages, newest first. Not available to bots."""
        peer = await self.resolve_peer(chat_id)
        seen = 0
        while True:
            page = 100 if not limit else min(100, limit - seen)
            result = await self.invoke(raw.functions.messages.GetHistory(
                peer=peer, offset_id=offset_id, offset_date=0, add_offset=0,
                limit=page, max_id=0, min_id=0, hash=0,
            ))
            messages = self._messages_from(result)
            if not messages:
                return
            for m in messages:
                yield m
                seen += 1
                if limit and seen >= limit:
                    return
            offset_id = messages[-1].id

    async def send_chat_action(self, chat_id: ChatId, action: str = "typing",
                               message_thread_id: Optional[int] = None) -> bool:
        """Show "typing...", "sending photo..." and so on, for ~5 seconds."""
        if action in CHAT_ACTIONS:
            kind = CHAT_ACTIONS[action]()
        elif action in UPLOAD_ACTIONS:
            kind = UPLOAD_ACTIONS[action](progress=0)
        else:
            raise ValueError(f"Unknown action {action!r}; use one of "
                             f"{', '.join([*CHAT_ACTIONS, *UPLOAD_ACTIONS])}")
        return await self.invoke(raw.functions.messages.SetTyping(
            peer=await self.resolve_peer(chat_id), action=kind, top_msg_id=message_thread_id))

    async def pin_chat_message(self, chat_id: ChatId, message_id: int,
                               disable_notification: bool = False, both_sides: bool = True) -> None:
        await self.invoke(raw.functions.messages.UpdatePinnedMessage(
            peer=await self.resolve_peer(chat_id), id=message_id,
            silent=disable_notification or None, pm_oneside=(not both_sides) or None,
        ))

    async def unpin_chat_message(self, chat_id: ChatId, message_id: int) -> None:
        await self.invoke(raw.functions.messages.UpdatePinnedMessage(
            peer=await self.resolve_peer(chat_id), id=message_id, unpin=True))


def _peer_of(input_peer, me_id):
    if isinstance(input_peer, raw.types.InputPeerSelf):
        return raw.types.PeerUser(user_id=me_id)
    return input_peer
