"""Peer ids and other small helpers.

wirelet uses Bot API style ids so they are interchangeable with other tools:
users are positive, basic groups are ``-chat_id`` and channels/supergroups are
``-100<channel_id>``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import List, Optional, Union

from wirelet import raw

CHANNEL_SHIFT = -1_000_000_000_000
MAX_CHAT_ID = 999_999_999_999


def peer_type(peer_id: int) -> str:
    if peer_id > 0:
        return "user"
    if peer_id >= -MAX_CHAT_ID:
        return "chat"
    return "channel"


def to_raw_id(peer_id: int) -> int:
    kind = peer_type(peer_id)
    if kind == "user":
        return peer_id
    if kind == "chat":
        return -peer_id
    return CHANNEL_SHIFT - peer_id


def get_peer_id(peer) -> int:
    """Bot API style id for any raw ``Peer``, ``InputPeer``, ``User`` or ``Chat``."""
    if isinstance(peer, (raw.types.PeerUser, raw.types.InputPeerUser, raw.types.InputUser,
                         raw.types.InputPeerUserFromMessage)):
        return peer.user_id
    if isinstance(peer, (raw.types.PeerChat, raw.types.InputPeerChat)):
        return -peer.chat_id
    if isinstance(peer, (raw.types.PeerChannel, raw.types.InputPeerChannel, raw.types.InputChannel,
                         raw.types.InputPeerChannelFromMessage)):
        return CHANNEL_SHIFT - peer.channel_id
    if isinstance(peer, (raw.types.User, raw.types.UserEmpty)):
        return peer.id
    if isinstance(peer, (raw.types.Chat, raw.types.ChatEmpty, raw.types.ChatForbidden)):
        return -peer.id
    if isinstance(peer, (raw.types.Channel, raw.types.ChannelForbidden)):
        return CHANNEL_SHIFT - peer.id
    raise ValueError(f"Not a peer: {type(peer).__name__}")


def input_peer_from_row(peer_id: int, access_hash: int):
    kind = peer_type(peer_id)
    if kind == "user":
        return raw.types.InputPeerUser(user_id=peer_id, access_hash=access_hash)
    if kind == "chat":
        return raw.types.InputPeerChat(chat_id=-peer_id)
    return raw.types.InputPeerChannel(channel_id=to_raw_id(peer_id), access_hash=access_hash)


def to_input_user(peer) -> "raw.base.InputUser":
    if isinstance(peer, raw.types.InputPeerSelf):
        return raw.types.InputUserSelf()
    if isinstance(peer, raw.types.InputPeerUser):
        return raw.types.InputUser(user_id=peer.user_id, access_hash=peer.access_hash)
    if isinstance(peer, raw.types.InputPeerUserFromMessage):
        return raw.types.InputUserFromMessage(peer=peer.peer, msg_id=peer.msg_id, user_id=peer.user_id)
    raise ValueError("This peer is not a user")


def to_input_channel(peer) -> "raw.base.InputChannel":
    if isinstance(peer, raw.types.InputPeerChannel):
        return raw.types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
    if isinstance(peer, raw.types.InputPeerChannelFromMessage):
        return raw.types.InputChannelFromMessage(peer=peer.peer, msg_id=peer.msg_id, channel_id=peer.channel_id)
    raise ValueError("This peer is not a channel")


def peer_rows(users: List, chats: List) -> List[tuple]:
    """Rows for :meth:`Storage.update_peers` from the users/chats of a response."""
    rows = []
    for u in users or []:
        if isinstance(u, raw.types.User) and u.access_hash is not None and not u.min:
            rows.append((u.id, u.access_hash, "bot" if u.bot else "user",
                         _username(u), u.phone))
    for c in chats or []:
        if isinstance(c, raw.types.Chat):
            rows.append((-c.id, 0, "group", None, None))
        elif isinstance(c, raw.types.Channel) and c.access_hash is not None and not c.min:
            kind = "supergroup" if c.megagroup else "channel"
            rows.append((CHANNEL_SHIFT - c.id, c.access_hash, kind, _username(c), None))
        elif isinstance(c, raw.types.ChannelForbidden):
            rows.append((CHANNEL_SHIFT - c.id, c.access_hash, "channel", None, None))
    return rows


def _username(obj) -> Optional[str]:
    if getattr(obj, "username", None):
        return obj.username
    for u in getattr(obj, "usernames", None) or []:
        if u.active:
            return u.username
    return None


def random_id() -> int:
    return int.from_bytes(os.urandom(8), "little", signed=True)


def timestamp_to_datetime(ts: Optional[int]) -> Optional[datetime]:
    return datetime.fromtimestamp(ts, timezone.utc) if ts else None


def datetime_to_timestamp(value: Union[datetime, int, None]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(value)


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2
