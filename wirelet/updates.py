"""Receiving updates reliably.

Telegram numbers updates with ``pts`` (per account, and per channel for
channels). When a number is skipped, something was lost in transit, so the
client asks for the difference instead of silently missing it:
https://core.telegram.org/api/updates
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from wirelet import errors, raw, utils
from wirelet.dispatcher import (
    CALLBACK_QUERY, DELETED_MESSAGES, EDITED_MESSAGE, INLINE_QUERY, MESSAGE, RAW_UPDATE,
)

log = logging.getLogger(__name__)

COMMON_BOX = 0


class UpdatesMixin:
    SAVE_STATE_EVERY = 10

    def __init__(self) -> None:
        self._pts: Optional[int] = None
        self._qts: int = 0
        self._date: int = 0
        self._seq: int = 0
        self._channel_pts: Dict[int, int] = {}
        self._diff_lock = asyncio.Lock()
        self._channel_diffs: Set[int] = set()
        self._state_task: Optional[asyncio.Task] = None
        self._updates_ready = False

    # ------------------------------------------------------------------ #
    # Start / stop
    # ------------------------------------------------------------------ #

    async def _start_updates(self) -> None:
        saved = await self.storage.get_state(COMMON_BOX)

        if saved and not self.skip_updates:
            self._pts, self._qts, self._date, self._seq = saved
            self._updates_ready = True
            await self._get_difference()
        else:
            state = await self.invoke(raw.functions.updates.GetState())
            self._pts, self._qts, self._date, self._seq = state.pts, state.qts, state.date, state.seq
            self._updates_ready = True

        self._state_task = asyncio.create_task(self._save_state_loop())

    async def _stop_updates(self) -> None:
        if self._state_task is not None:
            self._state_task.cancel()
            try:
                await self._state_task
            except asyncio.CancelledError:
                pass
            self._state_task = None
        await self._save_state()
        self._updates_ready = False

    async def _save_state(self) -> None:
        if self._pts is not None:
            await self.storage.set_state(COMMON_BOX, self._pts, self._qts, self._date, self._seq)

    async def _save_state_loop(self) -> None:
        while True:
            await asyncio.sleep(self.SAVE_STATE_EVERY)
            try:
                await self._save_state()
            except Exception:  # noqa: BLE001
                log.exception("Could not save update state")

    async def _on_new_session(self) -> None:
        # The server may have dropped updates around a session change.
        if self._updates_ready:
            await self._get_difference()

    # ------------------------------------------------------------------ #
    # Incoming
    # ------------------------------------------------------------------ #

    async def _on_raw_updates(self, updates: Any) -> None:
        if not self._updates_ready or self.no_updates:
            return
        try:
            await self._handle_updates(updates)
        except Exception:  # noqa: BLE001
            log.exception("Failed to handle updates")

    async def _handle_updates(self, updates: Any) -> None:
        t = raw.types

        if isinstance(updates, t.UpdatesTooLong):
            await self._get_difference()
            return

        if isinstance(updates, (t.Updates, t.UpdatesCombined)):
            await self._remember_peers(updates)
            users = {u.id: u for u in updates.users}
            chats = {c.id: c for c in updates.chats}
            for update in updates.updates:
                await self._apply(update, users, chats)
            self._date = max(self._date, updates.date)
            return

        if isinstance(updates, t.UpdateShort):
            await self._apply(updates.update, {}, {})
            self._date = max(self._date, updates.date)
            return

        if isinstance(updates, (t.UpdateShortMessage, t.UpdateShortChatMessage)):
            # Short updates omit the sender. If we never saw them, let
            # getDifference deliver this message with full user info instead
            # (pts is not advanced, so the difference still contains it).
            sender = updates.user_id if isinstance(updates, t.UpdateShortMessage) else updates.from_id
            if self._pts is not None and await self.storage.get_peer_by_id(sender) is None:
                await self._get_difference_soon()
                return
            if not await self._accept_pts(updates.pts, updates.pts_count):
                return
            message = self._short_to_message(updates)
            await self._emit(t.UpdateNewMessage(message=message, pts=updates.pts, pts_count=0), {}, {})
            return

        if isinstance(updates, t.UpdateShortSentMessage):
            await self._accept_pts(updates.pts, updates.pts_count)
            return

    def _short_to_message(self, u) -> "raw.types.Message":
        t = raw.types
        if isinstance(u, t.UpdateShortMessage):
            peer = t.PeerUser(user_id=u.user_id)
            from_id = t.PeerUser(user_id=self.me_id if u.out else u.user_id)
        else:
            peer = t.PeerChat(chat_id=u.chat_id)
            from_id = t.PeerUser(user_id=u.from_id)
        return t.Message(
            id=u.id, peer_id=peer, from_id=from_id, date=u.date, message=u.message,
            out=u.out, mentioned=u.mentioned, media_unread=u.media_unread, silent=u.silent,
            fwd_from=u.fwd_from, via_bot_id=u.via_bot_id, reply_to=u.reply_to,
            entities=u.entities, ttl_period=u.ttl_period,
        )

    # -- sequencing ------------------------------------------------------ #

    async def _accept_pts(self, pts: int, count: int) -> bool:
        """True if an update with this pts is the next one; handles gaps."""
        if self._pts is None:
            self._pts = pts
            return True
        expected = self._pts + count
        if expected == pts:
            self._pts = pts
            return True
        if expected > pts:
            return False  # already seen
        await self._get_difference_soon()
        return False

    async def _accept_channel_pts(self, channel_id: int, pts: int, count: int) -> bool:
        local = self._channel_pts.get(channel_id)
        if local is None:
            self._channel_pts[channel_id] = pts
            return True
        expected = local + count
        if expected == pts:
            self._channel_pts[channel_id] = pts
            return True
        if expected > pts:
            return False
        asyncio.create_task(self._get_channel_difference(channel_id))
        return False

    async def _apply(self, update: Any, users: Dict, chats: Dict) -> None:
        t = raw.types
        channel_id = _channel_of(update)

        if isinstance(update, t.UpdateChannelTooLong):
            asyncio.create_task(self._get_channel_difference(update.channel_id))
            return

        pts = getattr(update, "pts", None)
        count = getattr(update, "pts_count", None)
        if pts is not None and count is not None:
            if channel_id is not None:
                if not await self._accept_channel_pts(channel_id, pts, count):
                    return
            elif not await self._accept_pts(pts, count):
                return

        qts = getattr(update, "qts", None)
        if qts is not None and channel_id is None:
            self._qts = max(self._qts, qts)

        await self._emit(update, users, chats)

    # -- getDifference ---------------------------------------------------- #

    async def _get_difference_soon(self) -> None:
        if not self._diff_lock.locked():
            asyncio.create_task(self._get_difference())

    async def _get_difference(self) -> None:
        if self._diff_lock.locked() or self._pts is None:
            return
        async with self._diff_lock:
            t = raw.types
            while True:
                try:
                    diff = await self.invoke(raw.functions.updates.GetDifference(
                        pts=self._pts, date=self._date, qts=self._qts, pts_total_limit=10000,
                    ))
                except errors.RPCError as e:
                    log.warning("getDifference failed: %s", e)
                    return

                if isinstance(diff, t.updates.DifferenceEmpty):
                    self._date, self._seq = diff.date, diff.seq
                    return
                if isinstance(diff, t.updates.DifferenceTooLong):
                    self._pts = diff.pts
                    continue

                users = {u.id: u for u in diff.users}
                chats = {c.id: c for c in diff.chats}
                for message in diff.new_messages:
                    await self._emit(t.UpdateNewMessage(message=message, pts=0, pts_count=0), users, chats)
                for update in diff.other_updates:
                    if isinstance(update, t.UpdateChannelTooLong):
                        asyncio.create_task(self._get_channel_difference(update.channel_id))
                        continue
                    channel_id = _channel_of(update)
                    if channel_id is not None and getattr(update, "pts", None) is not None:
                        self._channel_pts[channel_id] = update.pts
                    await self._emit(update, users, chats)

                state = diff.state if isinstance(diff, t.updates.Difference) else diff.intermediate_state
                self._pts, self._qts, self._date, self._seq = state.pts, state.qts, state.date, state.seq

                if isinstance(diff, t.updates.Difference):
                    return

    async def _get_channel_difference(self, channel_id: int) -> None:
        if channel_id in self._channel_diffs:
            return
        self._channel_diffs.add(channel_id)
        try:
            row = await self.storage.get_peer_by_id(utils.CHANNEL_SHIFT - channel_id)
            if row is None:
                return
            channel = raw.types.InputChannel(channel_id=channel_id, access_hash=row[1])
            t = raw.types

            while True:
                pts = self._channel_pts.get(channel_id)
                if pts is None:
                    return
                try:
                    diff = await self.invoke(raw.functions.updates.GetChannelDifference(
                        channel=channel, filter=t.ChannelMessagesFilterEmpty(),
                        pts=pts, limit=1000 if self.is_bot else 100, force=True,
                    ))
                except errors.RPCError as e:
                    log.debug("getChannelDifference(%s) failed: %s", channel_id, e)
                    return

                if isinstance(diff, t.updates.ChannelDifferenceEmpty):
                    self._channel_pts[channel_id] = diff.pts
                    return

                users = {u.id: u for u in diff.users}
                chats = {c.id: c for c in diff.chats}

                if isinstance(diff, t.updates.ChannelDifferenceTooLong):
                    dialog_pts = getattr(diff.dialog, "pts", None)
                    if dialog_pts:
                        self._channel_pts[channel_id] = dialog_pts
                    for message in diff.messages:
                        await self._emit(t.UpdateNewChannelMessage(message=message, pts=0, pts_count=0),
                                         users, chats)
                else:
                    for message in diff.new_messages:
                        await self._emit(t.UpdateNewChannelMessage(message=message, pts=0, pts_count=0),
                                         users, chats)
                    for update in diff.other_updates:
                        await self._emit(update, users, chats)
                    self._channel_pts[channel_id] = diff.pts

                if diff.final:
                    return
        finally:
            self._channel_diffs.discard(channel_id)

    # ------------------------------------------------------------------ #
    # Turning raw updates into handler calls
    # ------------------------------------------------------------------ #

    async def _emit(self, update: Any, users: Dict, chats: Dict) -> None:
        from wirelet.types import CallbackQuery, InlineQuery, Message, User

        t = raw.types
        d = self.dispatcher

        if d.has(RAW_UPDATE):
            d.feed(RAW_UPDATE, update, users, chats)

        if isinstance(update, (t.UpdateNewMessage, t.UpdateNewChannelMessage)):
            if d.has(MESSAGE) and not isinstance(update.message, t.MessageEmpty):
                d.feed(MESSAGE, Message._parse(self, update.message, users, chats))

        elif isinstance(update, (t.UpdateEditMessage, t.UpdateEditChannelMessage)):
            if d.has(EDITED_MESSAGE):
                d.feed(EDITED_MESSAGE, Message._parse(self, update.message, users, chats))

        elif isinstance(update, (t.UpdateDeleteMessages, t.UpdateDeleteChannelMessages)):
            if d.has(DELETED_MESSAGES):
                chat_id = (utils.CHANNEL_SHIFT - update.channel_id
                           if isinstance(update, t.UpdateDeleteChannelMessages) else None)
                d.feed(DELETED_MESSAGES, DeletedMessages(chat_id, list(update.messages)))

        elif isinstance(update, t.UpdateBotCallbackQuery):
            if d.has(CALLBACK_QUERY):
                message = None
                try:
                    found = await self.get_messages(utils.get_peer_id(update.peer), [update.msg_id])
                    message = found[0] if found else None
                except errors.RPCError as e:
                    log.debug("Could not load the message of a callback query: %s", e)
                d.feed(CALLBACK_QUERY, CallbackQuery(
                    id=update.query_id,
                    from_user=User._parse(self, users.get(update.user_id)) or User(id=update.user_id)._bind(self),
                    chat_instance=update.chat_instance,
                    data=_decode(update.data),
                    message=message,
                    game_short_name=update.game_short_name,
                    raw=update,
                )._bind(self))

        elif isinstance(update, t.UpdateInlineBotCallbackQuery):
            if d.has(CALLBACK_QUERY):
                d.feed(CALLBACK_QUERY, CallbackQuery(
                    id=update.query_id,
                    from_user=User._parse(self, users.get(update.user_id)) or User(id=update.user_id)._bind(self),
                    chat_instance=update.chat_instance,
                    data=_decode(update.data),
                    inline_message_id=update.msg_id,
                    game_short_name=update.game_short_name,
                    raw=update,
                )._bind(self))

        elif isinstance(update, t.UpdateBotInlineQuery):
            if d.has(INLINE_QUERY):
                d.feed(INLINE_QUERY, InlineQuery(
                    id=update.query_id,
                    from_user=User._parse(self, users.get(update.user_id)) or User(id=update.user_id)._bind(self),
                    query=update.query,
                    offset=update.offset,
                    chat_type=_inline_peer_type(update.peer_type),
                    raw=update,
                )._bind(self))


class DeletedMessages:
    """Message ids that were deleted. ``chat_id`` is None outside channels,
    because Telegram does not say which private chat or group they were in."""

    def __init__(self, chat_id: Optional[int], message_ids: List[int]) -> None:
        self.chat_id = chat_id
        self.message_ids = message_ids

    def __repr__(self) -> str:
        return f"DeletedMessages(chat_id={self.chat_id}, message_ids={self.message_ids})"


def _channel_of(update: Any) -> Optional[int]:
    t = raw.types
    if isinstance(update, (t.UpdateNewChannelMessage, t.UpdateEditChannelMessage)):
        peer = getattr(update.message, "peer_id", None)
        return peer.channel_id if isinstance(peer, t.PeerChannel) else None
    channel_id = getattr(update, "channel_id", None)
    if channel_id is not None and type(update).__name__.startswith(("UpdateChannel", "UpdateDeleteChannel",
                                                                     "UpdatePinnedChannel")):
        return channel_id
    return None


def _decode(data: Optional[bytes]):
    if data is None:
        return None
    try:
        return data.decode()
    except UnicodeDecodeError:
        return data


def _inline_peer_type(peer_type) -> Optional[str]:
    if peer_type is None:
        return None
    name = type(peer_type).__name__.removeprefix("InlineQueryPeerType")
    return {
        "SameBotPM": "sender", "PM": "private", "BotPM": "bot", "Chat": "group",
        "Megagroup": "supergroup", "Broadcast": "channel",
    }.get(name, name.lower())
