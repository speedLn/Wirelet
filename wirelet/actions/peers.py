"""Finding people and chats."""

from __future__ import annotations

import re
from typing import AsyncIterator, Union

from wirelet import faults, schema, utils
from wirelet.model import Chat, User
from wirelet.model.people import chat_from_chat, chat_from_peer, chat_from_user, user_from

Peer = Union[int, str, "schema.kinds.InputPeer"]

_LINK = re.compile(r"^(?:https?://)?(?:www\.)?(?:t(?:elegram)?\.(?:me|dog))/(?P<rest>.+)$", re.I)


def _is_input_peer(value) -> bool:
    return isinstance(value, schema.kinds.InputPeer)


class PeerActions:
    async def resolve(self, peer: Peer) -> "schema.kinds.InputPeer":
        """An ``InputPeer`` for an id, ``@handle``, ``t.me`` link, ``+phone`` or ``"me"``.

        An id only works for someone this account has already seen (in an
        update, a chat, a reply...), because Telegram needs a per-account hash.
        """
        o = schema.objects
        if _is_input_peer(peer):
            return peer

        if isinstance(peer, str):
            value = peer.strip()
            if value.lower() in ("me", "self"):
                return o.InputPeerSelf()
            link = _LINK.match(value)
            if link:
                value = link.group("rest").split("/")[0].split("?")[0]
            if value.lstrip("-").isdigit():
                return await self.resolve(int(value))
            if value.startswith("+") and value[1:].replace(" ", "").isdigit():
                return await self._by_phone(value)
            return await self._by_handle(value.lstrip("@"))

        if not isinstance(peer, int):
            raise TypeError(f"can't resolve {peer!r}")
        if peer == self.me_id:
            return o.InputPeerSelf()

        row = await self.storage.get_peer_by_id(peer)
        if row is not None:
            return utils.input_peer_from_row(row[0], row[1])

        kind = utils.peer_type(peer)
        if kind == "chat":
            return o.InputPeerChat(chat_id=-peer)

        # Bots may address users and channels with a zero access hash.
        try:
            if kind == "user":
                found = await self.call(schema.methods.users.GetUsers(id=[o.InputUser(user_id=peer, access_hash=0)]))
                if found and isinstance(found[0], o.User) and found[0].access_hash is not None:
                    return o.InputPeerUser(user_id=peer, access_hash=found[0].access_hash)
            else:
                found = await self.call(schema.methods.channels.GetChannels(
                    id=[o.InputChannel(channel_id=utils.to_raw_id(peer), access_hash=0)]))
                for c in found.chats:
                    if getattr(c, "access_hash", None) is not None:
                        return o.InputPeerChannel(channel_id=c.id, access_hash=c.access_hash)
        except faults.TelegramFault:
            pass

        raise faults.from_telegram(
            400, "PEER_ID_INVALID",
            f"resolve({peer}): this account hasn't seen that id yet - use a @handle once, "
            f"or wait for an update from them")

    async def _by_handle(self, handle: str):
        row = await self.storage.get_peer_by_username(handle)
        if row is not None:
            return utils.input_peer_from_row(row[0], row[1])
        result = await self.call(schema.methods.contacts.ResolveUsername(username=handle))
        if isinstance(result.peer, schema.objects.PeerChat):
            return schema.objects.InputPeerChat(chat_id=result.peer.chat_id)
        row = await self.storage.get_peer_by_id(utils.get_peer_id(result.peer))
        if row is None:
            raise faults.from_telegram(400, "USERNAME_NOT_OCCUPIED", "contacts.resolveUsername")
        return utils.input_peer_from_row(row[0], row[1])

    async def _by_phone(self, phone: str):
        phone = phone.lstrip("+").replace(" ", "")
        row = await self.storage.get_peer_by_phone(phone)
        if row is None:
            result = await self.call(schema.methods.contacts.ResolvePhone(phone=phone))
            row = await self.storage.get_peer_by_id(utils.get_peer_id(result.peer))
        if row is None:
            raise faults.from_telegram(400, "PHONE_NOT_OCCUPIED", "contacts.resolvePhone")
        return utils.input_peer_from_row(row[0], row[1])

    async def lookup(self, peer: Peer) -> Union[User, Chat]:
        """Who or what ``peer`` is: a :class:`User` for people and bots, else a :class:`Chat`."""
        o = schema.objects
        target = await self.resolve(peer)
        if isinstance(target, (o.InputPeerUser, o.InputPeerSelf, o.InputPeerUserFromMessage)):
            users = await self.call(schema.methods.users.GetUsers(id=[utils.to_input_user(target)]))
            return user_from(self, users[0])
        if isinstance(target, o.InputPeerChat):
            result = await self.call(schema.methods.messages.GetChats(id=[target.chat_id]))
        else:
            result = await self.call(schema.methods.channels.GetChannels(id=[utils.to_input_channel(target)]))
        return chat_from_chat(self, result.chats[0])

    async def chat_of(self, peer: Peer) -> Chat:
        """Like :meth:`lookup`, but always a :class:`Chat` (a user becomes their private chat)."""
        found = await self.lookup(peer)
        return chat_from_user(self, found.raw) if isinstance(found, User) else found

    async def join(self, target: str) -> Chat:
        """Join by ``@handle``, ``t.me/name`` or an invite link (``t.me/+...``)."""
        value = target.strip()
        link = _LINK.match(value)
        rest = link.group("rest") if link else value
        invite = re.match(r"^(?:\+|joinchat/)([\w-]+)", rest)
        if invite:
            result = await self.call(schema.methods.messages.ImportChatInvite(hash=invite.group(1)))
        else:
            channel = utils.to_input_channel(await self.resolve(value))
            result = await self.call(schema.methods.channels.JoinChannel(channel=channel))
        chats = getattr(getattr(result, "updates", None), "chats", None) or []
        if not chats:
            raise faults.from_telegram(400, "JOIN_RESULT_EMPTY", "join")
        return chat_from_chat(self, chats[0])

    async def leave(self, chat: Peer, *, clear: bool = False) -> None:
        target = await self.resolve(chat)
        if isinstance(target, schema.objects.InputPeerChat):
            await self.call(schema.methods.messages.DeleteChatUser(
                chat_id=target.chat_id, user_id=schema.objects.InputUserSelf()))
            if clear:
                await self.call(schema.methods.messages.DeleteHistory(peer=target, max_id=0))
        else:
            await self.call(schema.methods.channels.LeaveChannel(channel=utils.to_input_channel(target)))

    async def chats(self, limit: int = 0) -> AsyncIterator[Chat]:
        """Your chats, most recent first. Accounts only; bots can't list chats."""
        o = schema.objects
        offset_date, offset_id, offset_peer = 0, 0, o.InputPeerEmpty()
        count = 0
        while True:
            page = await self.call(schema.methods.messages.GetDialogs(
                offset_date=offset_date, offset_id=offset_id, offset_peer=offset_peer, limit=100, hash=0))
            users = {u.id: u for u in page.users}
            chats = {c.id: c for c in page.chats}
            if not page.dialogs:
                return
            for dialog in page.dialogs:
                chat = chat_from_peer(self, dialog.peer, users, chats)
                if chat is None:
                    continue
                yield chat
                count += 1
                if limit and count >= limit:
                    return
            if isinstance(page, o.messages.Dialogs):
                return
            last = page.dialogs[-1]
            tops = {(utils.get_peer_id(m.peer_id), m.id): m for m in page.messages
                    if not isinstance(m, o.MessageEmpty)}
            top = tops.get((utils.get_peer_id(last.peer), last.top_message))
            if top is None:
                return
            offset_date, offset_id = top.date, top.id
            offset_peer = await self.resolve(utils.get_peer_id(last.peer))
