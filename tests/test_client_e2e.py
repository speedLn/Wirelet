"""A whole bot session against the in-process MTProto server.

Covers: auth key creation, salt correction, initConnection with the current
layer, bot login, update delivery to a handler, replying with formatting,
flood-wait retry, RPC errors, pts gap recovery and reconnecting.
"""

import asyncio
import time

import pytest

from wirelet import Client, errors, filters, raw
from wirelet.network import dc as dcmod

from .mtproto_server import MockServer, install_test_key

BOT_ID, ALICE_ID = 111, 222

BOT = raw.types.User(id=BOT_ID, is_self=True, bot=True, bot_info_version=1, first_name="Test Bot",
                     username="test_bot", access_hash=1)
ALICE = raw.types.User(id=ALICE_ID, first_name="Alice", username="alice", access_hash=5)


class Telegram:
    """Answers requests like Telegram would, for this test's scenario."""

    def __init__(self) -> None:
        self.pts = 1
        self.next_id = 2
        self.flood_once = True
        self.sent = []

    def __call__(self, query, conn):
        f, t = raw.functions, raw.types
        now = int(time.time())

        if isinstance(query, f.help.GetConfig):
            return t.DataJSON(data="{}")
        if isinstance(query, f.auth.ImportBotAuthorization):
            assert query.bot_auth_token == "123:ABC"
            return t.auth.Authorization(user=BOT)
        if isinstance(query, f.users.GetUsers):
            return [BOT]
        if isinstance(query, f.updates.GetState):
            return t.updates.State(pts=self.pts, qts=0, date=now, seq=0, unread_count=0)
        if isinstance(query, f.updates.GetDifference):
            return t.updates.DifferenceEmpty(date=now, seq=0)
        if isinstance(query, f.messages.SendMessage):
            if self.flood_once:
                self.flood_once = False
                return t.RpcError(error_code=420, error_message="FLOOD_WAIT_0")
            self.sent.append(query)
            self.pts += 1
            msg_id, self.next_id = self.next_id, self.next_id + 1
            message = t.Message(id=msg_id, out=True, peer_id=t.PeerUser(user_id=ALICE_ID),
                                from_id=t.PeerUser(user_id=BOT_ID), date=now,
                                message=query.message, entities=query.entities)
            return t.Updates(
                updates=[t.UpdateMessageID(id=msg_id, random_id=query.random_id),
                         t.UpdateNewMessage(message=message, pts=self.pts, pts_count=1)],
                users=[BOT, ALICE], chats=[], date=now, seq=0,
            )
        if isinstance(query, f.contacts.ResolveUsername):
            if query.username == "alice":
                return t.contacts.ResolvedPeer(peer=t.PeerUser(user_id=ALICE_ID), chats=[], users=[ALICE])
            return t.RpcError(error_code=400, error_message="USERNAME_NOT_OCCUPIED")
        return t.RpcError(error_code=400, error_message="METHOD_NOT_MOCKED")


@pytest.fixture
async def setup(monkeypatch, tmp_path):
    install_test_key(monkeypatch)
    telegram = Telegram()
    server = await MockServer(telegram).start()
    monkeypatch.setattr(dcmod, "PRODUCTION", {2: [("127.0.0.1", server.port)]})

    client = Client("bot", api_id=1, api_hash="hash", bot_token="123:ABC", workdir=tmp_path)
    yield client, server, telegram
    if client.is_connected:
        await client.stop()
    await server.stop()


async def wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def test_bot_session_end_to_end(setup):
    client, server, telegram = setup
    seen = []

    @client.on_message(filters.command("start") & filters.private)
    async def start(c, message):
        seen.append(message)
        await message.reply(f"<b>Hi</b> {message.from_user.first_name}!")

    await client.start()

    # Handshake told the server which DC it is and logged in as the bot.
    assert server.seen_dc_ids == [2]
    assert client.me.id == BOT_ID and client.is_bot
    init = server.init_connections[0]
    assert init.api_id == 1
    assert any(isinstance(q, raw.functions.InvokeWithLayer) and q.layer == raw.LAYER
               for q in server.received)

    # Telegram pushes a message from Alice.
    conn = server.connections[0]
    telegram.pts += 1
    await conn.push(raw.types.Updates(
        updates=[raw.types.UpdateNewMessage(
            message=raw.types.Message(id=1, peer_id=raw.types.PeerUser(user_id=ALICE_ID),
                                      date=int(time.time()), message="/start hello \"big world\""),
            pts=telegram.pts, pts_count=1)],
        users=[ALICE], chats=[], date=int(time.time()), seq=0,
    ))

    await wait_for(lambda: telegram.sent)
    message = seen[0]
    assert message.text.startswith("/start")
    assert message.command == ["start", "hello", "big world"]
    assert message.from_user.id == ALICE_ID and message.chat.type == "private"

    reply = telegram.sent[0]
    assert reply.message == "Hi Alice!"
    assert isinstance(reply.entities[0], raw.types.MessageEntityBold)
    assert (reply.entities[0].offset, reply.entities[0].length) == (0, 2)
    assert reply.peer == raw.types.InputPeerUser(user_id=ALICE_ID, access_hash=5)
    assert reply.reply_to.reply_to_msg_id == 1

    # The first SendMessage got FLOOD_WAIT_0 and was retried transparently.
    sends = [q for q in server.received if isinstance(q, raw.functions.messages.SendMessage)]
    assert len(sends) == 2


async def test_rpc_errors_and_username_resolution(setup):
    client, server, telegram = setup
    await client.start()

    peer = await client.resolve_peer("@alice")
    assert peer == raw.types.InputPeerUser(user_id=ALICE_ID, access_hash=5)
    # Now cached: no second network round trip.
    before = len(server.received)
    await client.resolve_peer("alice")
    assert len(server.received) == before

    with pytest.raises(errors.UsernameNotOccupied) as exc:
        await client.resolve_peer("@nobody")
    assert isinstance(exc.value, errors.BadRequest)
    assert exc.value.code == 400

    sent = await client.send_message("alice", "plain **text**", parse_mode=None)
    assert sent.text == "plain **text**" and sent.outgoing


async def test_pts_gap_triggers_get_difference(setup):
    client, server, telegram = setup
    await client.start()

    await server.connections[0].push(raw.types.Updates(
        updates=[raw.types.UpdateDeleteMessages(messages=[9], pts=telegram.pts + 5, pts_count=1)],
        users=[], chats=[], date=int(time.time()), seq=0,
    ))
    await wait_for(lambda: any(isinstance(q, raw.functions.updates.GetDifference) for q in server.received))


async def test_reconnects_and_keeps_working(setup):
    client, server, telegram = setup
    await client.start()
    inits = len(server.init_connections)

    # Telegram drops the connection.
    server.connections[0].writer.close()
    await wait_for(lambda: len(server.init_connections) > inits)

    me = await client.get_me()
    assert me.id == BOT_ID


async def test_session_survives_restart_without_new_handshake(setup):
    client, server, telegram = setup
    await client.start()
    await client.stop()
    keys = len(server.keys)

    again = Client("bot", api_id=1, api_hash="hash", workdir=client.storage.path.parent)
    await again.start()
    try:
        assert len(server.keys) == keys  # reused the stored auth key
        assert again.me.id == BOT_ID
        string = again.export_session_string()
    finally:
        await again.stop()

    from_string = Client("x", session_string=string)
    await from_string.start()
    try:
        assert from_string.me.id == BOT_ID
    finally:
        await from_string.stop()
