"""Peer ids and other small helpers.

wirelet uses Bot API style ids so they are interchangeable with other tools:
users are positive, basic groups are ``-chat_id`` and channels/supergroups are
``-100<channel_id>``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import List, Optional, Union

from wirelet import schema

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
    if isinstance(peer, (schema.objects.PeerUser, schema.objects.InputPeerUser, schema.objects.InputUser,
                         schema.objects.InputPeerUserFromMessage)):
        return peer.user_id
    if isinstance(peer, (schema.objects.PeerChat, schema.objects.InputPeerChat)):
        return -peer.chat_id
    if isinstance(peer, (schema.objects.PeerChannel, schema.objects.InputPeerChannel, schema.objects.InputChannel,
                         schema.objects.InputPeerChannelFromMessage)):
        return CHANNEL_SHIFT - peer.channel_id
    if isinstance(peer, (schema.objects.User, schema.objects.UserEmpty)):
        return peer.id
    if isinstance(peer, (schema.objects.Chat, schema.objects.ChatEmpty, schema.objects.ChatForbidden)):
        return -peer.id
    if isinstance(peer, (schema.objects.Channel, schema.objects.ChannelForbidden)):
        return CHANNEL_SHIFT - peer.id
    raise ValueError(f"Not a peer: {type(peer).__name__}")


def input_peer_from_row(peer_id: int, access_hash: int):
    kind = peer_type(peer_id)
    if kind == "user":
        return schema.objects.InputPeerUser(user_id=peer_id, access_hash=access_hash)
    if kind == "chat":
        return schema.objects.InputPeerChat(chat_id=-peer_id)
    return schema.objects.InputPeerChannel(channel_id=to_raw_id(peer_id), access_hash=access_hash)


def to_input_user(peer) -> "schema.kinds.InputUser":
    if isinstance(peer, schema.objects.InputPeerSelf):
        return schema.objects.InputUserSelf()
    if isinstance(peer, schema.objects.InputPeerUser):
        return schema.objects.InputUser(user_id=peer.user_id, access_hash=peer.access_hash)
    if isinstance(peer, schema.objects.InputPeerUserFromMessage):
        return schema.objects.InputUserFromMessage(peer=peer.peer, msg_id=peer.msg_id, user_id=peer.user_id)
    raise ValueError("This peer is not a user")


def to_input_channel(peer) -> "schema.kinds.InputChannel":
    if isinstance(peer, schema.objects.InputPeerChannel):
        return schema.objects.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
    if isinstance(peer, schema.objects.InputPeerChannelFromMessage):
        return schema.objects.InputChannelFromMessage(peer=peer.peer, msg_id=peer.msg_id, channel_id=peer.channel_id)
    raise ValueError("This peer is not a channel")


def peer_rows(users: List, chats: List) -> List[tuple]:
    """Rows for :meth:`Storage.update_peers` from the users/chats of a response."""
    rows = []
    for u in users or []:
        if isinstance(u, schema.objects.User) and u.access_hash is not None and not u.min:
            rows.append((u.id, u.access_hash, "bot" if u.bot else "user",
                         handle_of(u), u.phone))
    for c in chats or []:
        if isinstance(c, schema.objects.Chat):
            rows.append((-c.id, 0, "group", None, None))
        elif isinstance(c, schema.objects.Channel) and c.access_hash is not None and not c.min:
            kind = "supergroup" if c.megagroup else "channel"
            rows.append((CHANNEL_SHIFT - c.id, c.access_hash, kind, handle_of(c), None))
        elif isinstance(c, schema.objects.ChannelForbidden):
            rows.append((CHANNEL_SHIFT - c.id, c.access_hash, "channel", None, None))
    return rows


def handle_of(obj) -> Optional[str]:
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
