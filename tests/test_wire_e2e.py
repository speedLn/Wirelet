"""A whole bot session against the in-process MTProto server.

Covers key agreement, salt correction, init with the current layer, bot login,
commands, conversations (ask), buttons and presses, listener priority / NEXT /
watchers / error listeners, flood waits, faults, pts gaps, reconnecting and
reusing a stored login.
"""

import asyncio
import time

import pytest

from wirelet import NEXT, Wire, faults, schema, ui
from wirelet.network import dc as dcmod

from .mtproto_server import MockServer, install_test_key

BOT_ID, ALICE_ID = 111, 222
o = schema.objects

BOT = o.User(id=BOT_ID, is_self=True, bot=True, bot_info_version=1, first_name="Test Bot",
             username="test_bot", access_hash=1)
ALICE = o.User(id=ALICE_ID, first_name="Alice", username="alice", access_hash=5)


class Telegram:
    def __init__(self) -> None:
        self.pts = 1
        self.next_id = 100
        self.flood_once = True
        self.sent = []
        self.acks = []

    def __call__(self, query, conn):
        m = schema.methods
        now = int(time.time())
        if isinstance(query, m.help.GetConfig):
            return o.DataJSON(data="{}")
        if isinstance(query, m.auth.ImportBotAuthorization):
            assert query.bot_auth_token == "123:ABC"
            return o.auth.Authorization(user=BOT)
        if isinstance(query, m.users.GetUsers):
            return [BOT]
        if isinstance(query, m.updates.GetState):
            return o.updates.State(pts=self.pts, qts=0, date=now, seq=0, unread_count=0)
        if isinstance(query, m.updates.GetDifference):
            return o.updates.DifferenceEmpty(date=now, seq=0)
        if isinstance(query, m.messages.SetBotCallbackAnswer):
            self.acks.append(query)
            return True
        if isinstance(query, m.messages.GetMessages):
            return o.messages.Messages(messages=[o.Message(
                id=query.id[0].id, out=True, peer_id=o.PeerUser(user_id=ALICE_ID),
                from_id=o.PeerUser(user_id=BOT_ID), date=now, message="Pick one")], topics=[], chats=[], users=[ALICE, BOT])
        if isinstance(query, m.messages.SendMessage):
            if self.flood_once:
                self.flood_once = False
                return o.RpcError(error_code=420, error_message="FLOOD_WAIT_0")
            self.sent.append(query)
            self.pts += 1
            msg_id, self.next_id = self.next_id, self.next_id + 1
            message = o.Message(id=msg_id, out=True, peer_id=o.PeerUser(user_id=ALICE_ID),
                                from_id=o.PeerUser(user_id=BOT_ID), date=now, message=query.message,
                                entities=query.entities, reply_markup=query.reply_markup)
            return o.Updates(updates=[o.UpdateMessageID(id=msg_id, random_id=query.random_id),
                                      o.UpdateNewMessage(message=message, pts=self.pts, pts_count=1)],
                             users=[BOT, ALICE], chats=[], date=now, seq=0)
        if isinstance(query, m.contacts.ResolveUsername):
            if query.username == "alice":
                return o.contacts.ResolvedPeer(peer=o.PeerUser(user_id=ALICE_ID), chats=[], users=[ALICE])
            return o.RpcError(error_code=400, error_message="USERNAME_NOT_OCCUPIED")
        return o.RpcError(error_code=400, error_message="METHOD_NOT_MOCKED")

    async def from_alice(self, conn, text, msg_id=1):
        self.pts += 1
        await conn.push(o.Updates(
            updates=[o.UpdateNewMessage(message=o.Message(
                id=msg_id, peer_id=o.PeerUser(user_id=ALICE_ID), date=int(time.time()), message=text),
                pts=self.pts, pts_count=1)],
            users=[ALICE], chats=[], date=int(time.time()), seq=0))


@pytest.fixture
async def env(monkeypatch, tmp_path):
    install_test_key(monkeypatch)
    telegram = Telegram()
    server = await MockServer(telegram).start()
    monkeypatch.setattr(dcmod, "PRODUCTION", {2: [("127.0.0.1", server.port)]})
    wire = Wire("bot", app_id=1, app_hash="hash", bot="123:ABC", folder=tmp_path)
    yield wire, server, telegram
    if wire.connected:
        await wire.stop()
    await server.stop()


async def until(predicate, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def test_command_reply_with_format_buttons_and_flood_retry(env):
    wire, server, telegram = env
    seen = []

    @wire.command("start", chat="dm")
    async def start(msg):
        seen.append(msg)
        await msg.quote(f"<b>Hi</b> {msg.author.first}!", buttons=ui.inline(
            [ui.btn("Yes", "yes", style="success"), ui.url("Site", "https://e.com")]))

    await wire.start()
    assert server.seen_dc_ids == [2]
    assert wire.me.id == BOT_ID and wire.is_bot and wire.me.handle == "test_bot"
    assert server.init_connections[0].api_id == 1
    assert any(isinstance(q, schema.methods.InvokeWithLayer) and q.layer == schema.LAYER
               for q in server.received)

    await telegram.from_alice(server.connections[0], '/start hello "big world"')
    await until(lambda: telegram.sent)

    msg = seen[0]
    assert (msg.cmd, msg.args, msg.rest) == ("start", ["hello", "big world"], 'hello "big world"')
    assert msg.author.id == ALICE_ID and msg.chat.kind == "dm" and msg.kind == "text"

    reply = telegram.sent[0]
    assert reply.message == "Hi Alice!"
    assert isinstance(reply.entities[0], o.MessageEntityBold)
    assert reply.peer == o.InputPeerUser(user_id=ALICE_ID, access_hash=5)
    assert reply.reply_to.reply_to_msg_id == 1
    row = reply.reply_markup.rows[0].buttons
    assert row[0].type.data == b"yes" and row[0].style.bg_success
    assert row[1].type.url == "https://e.com"

    # FLOOD_WAIT_0 on the first send was waited out and retried.
    assert len([q for q in server.received if isinstance(q, schema.methods.messages.SendMessage)]) == 2


async def test_ask_conversation(env):
    wire, server, telegram = env
    telegram.flood_once = False
    answers = []

    @wire.command("name")
    async def name(msg):
        reply = await msg.ask("What's your name?", timeout=5)
        answers.append(reply.text)
        await msg.say(f"Nice to meet you, {reply.text}")

    @wire.on.message()
    async def everything_else(msg):
        answers.append(("listener", msg.text))

    await wire.start()
    conn = server.connections[0]
    await telegram.from_alice(conn, "/name", msg_id=1)
    await until(lambda: len(telegram.sent) == 1)
    await telegram.from_alice(conn, "Bob", msg_id=2)
    await until(lambda: len(telegram.sent) == 2)

    assert answers == ["Bob"]  # the answer went to ask(), not to the catch-all listener
    assert telegram.sent[1].message == "Nice to meet you, Bob"


async def test_priority_next_watchers_and_error_listener(env):
    wire, server, telegram = env
    calls, errors = [], []

    @wire.on.message(watch=True)
    async def log_everything(msg):
        calls.append("watch")

    @wire.on.message(priority=10, pattern=r"\d+")
    async def numbers_first(msg):
        calls.append(("numbers", msg.match.group()))
        return NEXT

    @wire.on.message(text="boom")
    async def explodes(msg):
        raise RuntimeError("kaboom")

    @wire.on.message()
    async def fallback(msg):
        calls.append(("fallback", msg.text))

    @wire.on.error
    async def on_error(error, update):
        errors.append((str(error), update.text))

    await wire.start()
    conn = server.connections[0]
    await telegram.from_alice(conn, "order 42", msg_id=1)
    await until(lambda: ("fallback", "order 42") in calls)
    await telegram.from_alice(conn, "boom", msg_id=2)
    await until(lambda: errors)

    assert ("numbers", "42") in calls and calls.count("watch") == 2
    assert errors == [("kaboom", "boom")]
    assert ("fallback", "boom") not in calls  # explodes() handled it, even though it failed


async def test_button_press(env):
    wire, server, telegram = env
    presses = []

    @wire.on.press("yes")
    async def yes(press):
        presses.append(press)
        await press.ack("Thanks!", alert=True)

    await wire.start()
    await server.connections[0].push(o.UpdateShort(update=o.UpdateBotCallbackQuery(
        query_id=77, user_id=ALICE_ID, peer=o.PeerUser(user_id=ALICE_ID), msg_id=100,
        chat_instance=1, data=b"yes"), date=int(time.time())))
    await until(lambda: telegram.acks)

    press = presses[0]
    assert press.data == "yes" and press.author.id == ALICE_ID
    assert press.msg.id == 100 and press.chat.id == ALICE_ID
    assert telegram.acks[0].query_id == 77 and telegram.acks[0].message == "Thanks!" and telegram.acks[0].alert


async def test_faults_and_resolving(env):
    wire, server, telegram = env
    await wire.start()

    peer = await wire.resolve("@alice")
    assert peer == o.InputPeerUser(user_id=ALICE_ID, access_hash=5)
    before = len(server.received)
    await wire.resolve("https://t.me/alice")
    assert len(server.received) == before  # cached

    with pytest.raises(faults.USERNAME_NOT_OCCUPIED) as caught:
        await wire.resolve("@nobody")
    assert isinstance(caught.value, faults.Rejected) and caught.value.code == 400

    sent = await wire.send("alice", "plain **text**", format=None)
    assert sent.text == "plain **text**" and sent.from_me

    with pytest.raises(ValueError):
        await wire.send("alice")


async def test_pts_gap_fetches_difference(env):
    wire, server, telegram = env
    await wire.start()
    await server.connections[0].push(o.Updates(
        updates=[o.UpdateDeleteMessages(messages=[9], pts=telegram.pts + 5, pts_count=1)],
        users=[], chats=[], date=int(time.time()), seq=0))
    await until(lambda: any(isinstance(q, schema.methods.updates.GetDifference) for q in server.received))


async def test_reconnects(env):
    wire, server, telegram = env
    await wire.start()
    inits = len(server.init_connections)
    server.connections[0].writer.close()
    await until(lambda: len(server.init_connections) > inits)
    assert (await wire.whoami()).id == BOT_ID


async def test_stored_login_and_login_key(env):
    wire, server, telegram = env
    await wire.start()
    await wire.stop()
    assert (wire.storage.path.name, wire.storage.path.exists()) == ("bot.wire", True)
    keys = len(server.keys)

    again = Wire("bot", folder=wire.storage.path.parent)
    async with again:
        assert len(server.keys) == keys and again.me.id == BOT_ID
        key = again.export_key()

    async with Wire(key=key) as from_key:
        assert from_key.me.id == BOT_ID
