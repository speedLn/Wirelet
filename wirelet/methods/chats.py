from __future__ import annotations

import re
from typing import AsyncGenerator, List, Union

from wirelet import errors, raw, utils
from wirelet.types import Chat, User

PeerLike = Union[int, str, "raw.base.InputPeer"]

_LINK = re.compile(r"^(?:https?://)?(?:www\.)?(?:t(?:elegram)?\.(?:me|dog))/(?P<rest>.+)$", re.I)


class Chats:
    async def resolve_peer(self, peer: PeerLike) -> "raw.base.InputPeer":
        """Turn an id, ``@username``, ``t.me`` link or phone number into an InputPeer.

        Ids must belong to someone this account has seen (sent you a message,
        is in a shared chat...), since Telegram needs a per-account access hash.
        """
        t = raw.types
        if isinstance(peer, TLObjectTypes):
            return peer

        if isinstance(peer, str):
            value = peer.strip()
            if value.lower() in ("me", "self"):
                return t.InputPeerSelf()
            link = _LINK.match(value)
            if link:
                value = link.group("rest").split("/")[0].split("?")[0]
            if value.lstrip("-").isdigit():
                return await self.resolve_peer(int(value))
            if value.startswith("+") and value[1:].replace(" ", "").isdigit():
                return await self._resolve_phone(value)
            return await self._resolve_username(value.lstrip("@"))

        if not isinstance(peer, int):
            raise TypeError(f"Can't resolve {peer!r}")

        if peer == self.me_id:
            return t.InputPeerSelf()

        row = await self.storage.get_peer_by_id(peer)
        if row is not None:
            return utils.input_peer_from_row(row[0], row[1])

        kind = utils.peer_type(peer)
        if kind == "chat":
            return t.InputPeerChat(chat_id=-peer)

        # Bots can address people and channels with a zero access hash.
        try:
            if kind == "user":
                users = await self.invoke(raw.functions.users.GetUsers(
                    id=[t.InputUser(user_id=peer, access_hash=0)]))
                if users and isinstance(users[0], t.User) and users[0].access_hash is not None:
                    return t.InputPeerUser(user_id=peer, access_hash=users[0].access_hash)
            else:
                result = await self.invoke(raw.functions.channels.GetChannels(
                    id=[t.InputChannel(channel_id=utils.to_raw_id(peer), access_hash=0)]))
                for c in result.chats:
                    if getattr(c, "access_hash", None) is not None:
                        return t.InputPeerChannel(channel_id=c.id, access_hash=c.access_hash)
        except errors.RPCError:
            pass

        raise errors.PeerIdInvalid(
            f"PEER_ID_INVALID: wirelet has not seen {peer} yet. Resolve it by username first, "
            f"or wait for an update from that chat.", 400)

    async def _resolve_username(self, username: str) -> "raw.base.InputPeer":
        row = await self.storage.get_peer_by_username(username)
        if row is not None:
            return utils.input_peer_from_row(row[0], row[1])

        result = await self.invoke(raw.functions.contacts.ResolveUsername(username=username))
        peer_id = utils.get_peer_id(result.peer)
        row = await self.storage.get_peer_by_id(peer_id)
        if row is not None:
            return utils.input_peer_from_row(row[0], row[1])
        if isinstance(result.peer, raw.types.PeerChat):
            return raw.types.InputPeerChat(chat_id=result.peer.chat_id)
        raise errors.UsernameNotOccupied("USERNAME_NOT_OCCUPIED", 400)

    async def _resolve_phone(self, phone: str) -> "raw.base.InputPeer":
        phone = phone.lstrip("+").replace(" ", "")
        row = await self.storage.get_peer_by_phone(phone)
        if row is not None:
            return utils.input_peer_from_row(row[0], row[1])
        result = await self.invoke(raw.functions.contacts.ResolvePhone(phone=phone))
        return utils.input_peer_from_row(*(await self.storage.get_peer_by_id(utils.get_peer_id(result.peer)))[:2])

    async def get_users(self, user_ids: Union[PeerLike, List[PeerLike]]) -> Union[User, List[User]]:
        single = not isinstance(user_ids, list)
        ids = [user_ids] if single else user_ids
        inputs = [utils.to_input_user(await self.resolve_peer(i)) for i in ids]
        users = await self.invoke(raw.functions.users.GetUsers(id=inputs))
        parsed = [User._parse(self, u) for u in users]
        return parsed[0] if single else parsed

    async def get_chat(self, chat_id: PeerLike) -> Chat:
        peer = await self.resolve_peer(chat_id)
        t = raw.types
        if isinstance(peer, (t.InputPeerUser, t.InputPeerSelf)):
            users = await self.invoke(raw.functions.users.GetUsers(id=[utils.to_input_user(peer)]))
            return Chat._parse_user(self, users[0])
        if isinstance(peer, t.InputPeerChat):
            result = await self.invoke(raw.functions.messages.GetChats(id=[peer.chat_id]))
            return Chat._parse_chat(self, result.chats[0])
        result = await self.invoke(raw.functions.channels.GetChannels(id=[utils.to_input_channel(peer)]))
        return Chat._parse_chat(self, result.chats[0])

    async def join_chat(self, chat: str) -> Chat:
        """Join by ``@username``, ``t.me/name`` or an invite link (``t.me/+hash``)."""
        value = chat.strip()
        link = _LINK.match(value)
        rest = link.group("rest") if link else value
        invite = re.match(r"^(?:\+|joinchat/)([\w-]+)", rest)

        if invite:
            result = await self.invoke(raw.functions.messages.ImportChatInvite(hash=invite.group(1)))
        else:
            peer = await self.resolve_peer(value)
            result = await self.invoke(raw.functions.channels.JoinChannel(channel=utils.to_input_channel(peer)))

        updates = getattr(result, "updates", None)
        chats = getattr(updates, "chats", None) or []
        if not chats:
            raise errors.named("ChatJoinFailed")("Telegram did not return the joined chat", 400)
        return Chat._parse_chat(self, chats[0])

    async def leave_chat(self, chat_id: PeerLike, delete: bool = False) -> None:
        peer = await self.resolve_peer(chat_id)
        if isinstance(peer, raw.types.InputPeerChat):
            await self.invoke(raw.functions.messages.DeleteChatUser(
                chat_id=peer.chat_id, user_id=raw.types.InputUserSelf()))
            if delete:
                await self.invoke(raw.functions.messages.DeleteHistory(peer=peer, max_id=0))
        else:
            await self.invoke(raw.functions.channels.LeaveChannel(channel=utils.to_input_channel(peer)))

    async def get_dialogs(self, limit: int = 0) -> AsyncGenerator[Chat, None]:
        """Iterate over your chats, newest first. Not available to bots."""
        t = raw.types
        offset_date, offset_id, offset_peer = 0, 0, t.InputPeerEmpty()
        seen = 0
        while True:
            result = await self.invoke(raw.functions.messages.GetDialogs(
                offset_date=offset_date, offset_id=offset_id, offset_peer=offset_peer,
                limit=100, hash=0,
            ))
            users = {u.id: u for u in result.users}
            chats = {c.id: c for c in result.chats}
            messages = {(utils.get_peer_id(m.peer_id), m.id): m for m in result.messages
                        if not isinstance(m, t.MessageEmpty)}

            if not result.dialogs:
                return
            for dialog in result.dialogs:
                chat = Chat._parse_peer(self, dialog.peer, users, chats)
                if chat is None:
                    continue
                yield chat
                seen += 1
                if limit and seen >= limit:
                    return

            if isinstance(result, t.messages.Dialogs):
                return
            last = result.dialogs[-1]
            last_msg = messages.get((utils.get_peer_id(last.peer), last.top_message))
            if last_msg is None:
                return
            offset_date, offset_id = last_msg.date, last_msg.id
            offset_peer = await self.resolve_peer(utils.get_peer_id(last.peer))


TLObjectTypes = (raw.types.InputPeerEmpty, raw.types.InputPeerSelf, raw.types.InputPeerChat,
                 raw.types.InputPeerUser, raw.types.InputPeerChannel,
                 raw.types.InputPeerUserFromMessage, raw.types.InputPeerChannelFromMessage)
