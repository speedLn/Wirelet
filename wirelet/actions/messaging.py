"""Sending, editing and reading messages."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import AsyncIterator, Iterable, List, Optional, Union

from wirelet import faults, parser, schema, utils
from wirelet.match import Constraints
from wirelet.model import Message
from wirelet.model.message import message_from

ChatRef = Union[int, str]
DEFAULT = "default"

ACTIVITIES = {
    "typing": lambda: schema.objects.SendMessageTypingAction(),
    "photo": lambda: schema.objects.SendMessageUploadPhotoAction(progress=0),
    "video": lambda: schema.objects.SendMessageUploadVideoAction(progress=0),
    "file": lambda: schema.objects.SendMessageUploadDocumentAction(progress=0),
    "voice": lambda: schema.objects.SendMessageRecordAudioAction(),
    "round": lambda: schema.objects.SendMessageRecordRoundAction(),
    "recording": lambda: schema.objects.SendMessageRecordVideoAction(),
    "sticker": lambda: schema.objects.SendMessageChooseStickerAction(),
    "location": lambda: schema.objects.SendMessageGeoLocationAction(),
    "contact": lambda: schema.objects.SendMessageChooseContactAction(),
    "game": lambda: schema.objects.SendMessageGamePlayAction(),
}


class Activity:
    """``await wire.typing(chat)`` shows "typing..." once (about 5 s);
    ``async with wire.typing(chat):`` keeps it up until the block ends."""

    def __init__(self, wire, chat: ChatRef, action: str) -> None:
        if action not in ACTIVITIES:
            raise ValueError(f"unknown activity {action!r}; use one of {', '.join(ACTIVITIES)}")
        self._wire, self._chat, self._action = wire, chat, action
        self._task: Optional[asyncio.Task] = None

    async def _send(self, action) -> None:
        await self._wire.call(schema.methods.messages.SetTyping(
            peer=await self._wire.resolve(self._chat), action=action))

    def __await__(self):
        return self._send(ACTIVITIES[self._action]()).__await__()

    async def _loop(self) -> None:
        while True:
            try:
                await self._send(ACTIVITIES[self._action]())
            except faults.TelegramFault:
                return
            await asyncio.sleep(4)

    async def __aenter__(self) -> "Activity":
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        try:
            await self._send(schema.objects.SendMessageCancelAction())
        except faults.TelegramFault:
            pass


def _ids(ids: Union[int, Iterable[int]]) -> List[int]:
    return [ids] if isinstance(ids, int) else list(ids)


class MessagingActions:
    # -- helpers --------------------------------------------------------- #

    async def _format(self, text: str, format: Optional[str], entities):
        """``(plain text, entities)`` for sending."""
        if entities is not None:
            return text, entities
        mode = self.format if format == DEFAULT else format
        plain, found, mentions = parser.parse(text or "", mode)
        for offset, length, user_id in mentions:
            try:
                user = utils.to_input_user(await self.resolve(user_id))
            except (faults.TelegramFault, ValueError):
                continue
            found.append(schema.objects.InputMessageEntityMentionName(offset=offset, length=length, user_id=user))
        found.sort(key=lambda e: e.offset)
        return plain, found or None

    async def _encode_buttons(self, buttons):
        return await buttons.encode(self) if buttons is not None else None

    @staticmethod
    def _reply_header(reply_to: Optional[int], topic: Optional[int]):
        if reply_to is None and topic is None:
            return None
        return schema.objects.InputReplyToMessage(reply_to_msg_id=reply_to or topic, top_msg_id=topic)

    def _messages_in(self, result) -> List[Message]:
        o = schema.objects
        users = {u.id: u for u in getattr(result, "users", [])}
        chats = {c.id: c for c in getattr(result, "chats", [])}
        if isinstance(result, (o.Updates, o.UpdatesCombined)):
            raws = [u.message for u in result.updates
                    if isinstance(u, (o.UpdateNewMessage, o.UpdateNewChannelMessage, o.UpdateNewScheduledMessage,
                                      o.UpdateEditMessage, o.UpdateEditChannelMessage))]
        elif isinstance(result, o.UpdateShort) and hasattr(result.update, "message"):
            raws = [result.update.message]
        else:
            raws = getattr(result, "messages", [])
        return [message_from(self, m, users, chats) for m in raws]

    async def _sent(self, result, peer) -> Message:
        if isinstance(result, schema.objects.UpdateShortSentMessage):
            # In private chats Telegram only returns the new id.
            target = self.me_id if isinstance(peer, schema.objects.InputPeerSelf) else utils.get_peer_id(peer)
            return await self.fetch(target, result.id)
        await self._remember_peers(result)
        found = self._messages_in(result)
        if not found:
            raise faults.from_telegram(500, "NO_MESSAGE_RETURNED", "send")
        return found[0]

    # -- sending ---------------------------------------------------------- #

    async def send(
        self,
        to: ChatRef,
        text: str = "",
        *,
        file=None,
        kind: Optional[str] = None,
        name: Optional[str] = None,
        mime: Optional[str] = None,
        thumb=None,
        buttons=None,
        reply_to: Optional[int] = None,
        topic: Optional[int] = None,
        format: Optional[str] = DEFAULT,
        entities: Optional[list] = None,
        preview: bool = True,
        silent: bool = False,
        protect: bool = False,
        at: Optional[datetime] = None,
        spoiler: bool = False,
        ttl: Optional[int] = None,
        duration: Optional[float] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        title: Optional[str] = None,
        performer: Optional[str] = None,
        progress=None,
    ) -> Message:
        """Send text, a file, or a file with ``text`` as its caption.

        ``to`` is a chat id, ``@handle``, ``t.me`` link or ``"me"``. ``file`` is
        a path, bytes, an open file, a URL, a :class:`Media` or a media ``ref``.
        ``kind`` (``photo video round gif audio voice sticker file``) is
        guessed from the file when left out. ``at`` schedules the message.
        """
        if file is None and not text:
            raise ValueError("send() needs text, a file, or both")

        peer = await self.resolve(to)
        message, found = await self._format(text, format, entities)
        common = dict(
            peer=peer, message=message, entities=found, random_id=utils.random_id(),
            reply_to=self._reply_header(reply_to, topic),
            reply_markup=await self._encode_buttons(buttons),
            silent=silent or None, noforwards=protect or None,
            schedule_date=utils.datetime_to_timestamp(at),
        )

        if file is None:
            result = await self.call(schema.methods.messages.SendMessage(no_webpage=(not preview) or None, **common))
        else:
            media = await self._input_media(
                file, kind, name=name, mime=mime, thumb=thumb, spoiler=spoiler, ttl=ttl,
                duration=duration, width=width, height=height, title=title, performer=performer,
                progress=progress,
            )
            result = await self.call(schema.methods.messages.SendMedia(media=media, **common))
        return await self._sent(result, peer)

    async def rewrite(self, chat: ChatRef, message_id: int, text: str, *, buttons=None,
                      format: Optional[str] = DEFAULT, entities: Optional[list] = None,
                      preview: bool = True) -> Message:
        """Edit a message's text or caption."""
        message, found = await self._format(text, format, entities)
        result = await self.call(schema.methods.messages.EditMessage(
            peer=await self.resolve(chat), id=message_id, message=message, entities=found,
            reply_markup=await self._encode_buttons(buttons), no_webpage=(not preview) or None,
        ))
        return self._messages_in(result)[0]

    async def rebutton(self, chat: ChatRef, message_id: int, buttons=None) -> Message:
        """Replace a message's buttons; ``None`` removes them."""
        markup = await self._encode_buttons(buttons) or schema.objects.ReplyInlineMarkup(rows=[])
        result = await self.call(schema.methods.messages.EditMessage(
            peer=await self.resolve(chat), id=message_id, reply_markup=markup))
        return self._messages_in(result)[0]

    async def rewrite_inline(self, inline_id, text: str, *, buttons=None, format: Optional[str] = DEFAULT,
                             entities: Optional[list] = None, preview: bool = True) -> bool:
        """Edit a message sent through this bot in inline mode."""
        message, found = await self._format(text, format, entities)
        session = await self._session_for(inline_id.dc_id, media=False)
        return await session.invoke(schema.methods.messages.EditInlineBotMessage(
            id=inline_id, message=message, entities=found,
            reply_markup=await self._encode_buttons(buttons), no_webpage=(not preview) or None,
        ))

    async def erase(self, chat: ChatRef, ids: Union[int, Iterable[int]], *, everyone: bool = True) -> int:
        """Delete messages. Returns how many were deleted."""
        peer = await self.resolve(chat)
        if isinstance(peer, schema.objects.InputPeerChannel):
            result = await self.call(schema.methods.channels.DeleteMessages(
                channel=utils.to_input_channel(peer), id=_ids(ids)))
        else:
            result = await self.call(schema.methods.messages.DeleteMessages(id=_ids(ids), revoke=everyone or None))
        return result.pts_count

    async def relay(self, to: ChatRef, chat: ChatRef, ids: Union[int, Iterable[int]], *,
                    anonymous: bool = False, silent: bool = False, protect: bool = False,
                    topic: Optional[int] = None) -> List[Message]:
        """Forward messages from ``chat`` to ``to``. ``anonymous`` sends copies
        without the "Forwarded from" header."""
        id_list = _ids(ids)
        result = await self.call(schema.methods.messages.ForwardMessages(
            from_peer=await self.resolve(chat), to_peer=await self.resolve(to), id=id_list,
            random_id=[utils.random_id() for _ in id_list], drop_author=anonymous or None,
            silent=silent or None, noforwards=protect or None, top_msg_id=topic,
        ))
        await self._remember_peers(result)
        return self._messages_in(result)

    async def pin(self, chat: ChatRef, message_id: int, *, silent: bool = False, for_both: bool = True) -> None:
        await self.call(schema.methods.messages.UpdatePinnedMessage(
            peer=await self.resolve(chat), id=message_id, silent=silent or None, pm_oneside=(not for_both) or None))

    async def unpin(self, chat: ChatRef, message_id: int) -> None:
        await self.call(schema.methods.messages.UpdatePinnedMessage(
            peer=await self.resolve(chat), id=message_id, unpin=True))

    def typing(self, chat: ChatRef, action: str = "typing") -> Activity:
        """Show an activity: ``typing photo video file voice round recording
        sticker location contact game``. Await it, or use ``async with``."""
        return Activity(self, chat, action)

    # -- reading ---------------------------------------------------------- #

    async def fetch(self, chat: ChatRef, ids: Union[int, Iterable[int]]):
        """Messages by id: one :class:`Message` for one id, a list for many.
        Messages that don't exist come back with ``exists=False``."""
        peer = await self.resolve(chat)
        wanted = [schema.objects.InputMessageID(id=i) for i in _ids(ids)]
        if isinstance(peer, schema.objects.InputPeerChannel):
            result = await self.call(schema.methods.channels.GetMessages(
                channel=utils.to_input_channel(peer), id=wanted))
        else:
            result = await self.call(schema.methods.messages.GetMessages(id=wanted))
        found = self._messages_in(result)
        return found[0] if isinstance(ids, int) else found

    async def history(self, chat: ChatRef, limit: int = 0, *, before: int = 0) -> AsyncIterator[Message]:
        """A chat's messages, newest first (older than ``before`` if given). Accounts only."""
        peer = await self.resolve(chat)
        count = 0
        while True:
            page = 100 if not limit else min(100, limit - count)
            result = await self.call(schema.methods.messages.GetHistory(
                peer=peer, offset_id=before, offset_date=0, add_offset=0,
                limit=page, max_id=0, min_id=0, hash=0))
            found = self._messages_in(result)
            if not found:
                return
            for m in found:
                yield m
                count += 1
                if limit and count >= limit:
                    return
            before = found[-1].id

    async def wait_message(self, *, chat: Optional[ChatRef] = None, by=None, timeout: Optional[float] = 120,
                           **constraints) -> Message:
        """Wait for the next message matching the constraints (see :mod:`wirelet.match`).

        A message that satisfies a wait goes to the waiter only, not to listeners.
        """
        if not self.listen:
            raise RuntimeError("wait_message needs updates; this wire was made with listen=False")
        return await self._router.wait(Constraints(chat=chat, by=by, **constraints), timeout)
