import asyncio
import os

import pytest

from wirelet import faults, parser, schema, ui, utils
from wirelet.crypto import aes, math as tlmath, mtproto
from wirelet.match import Command, Constraints
from wirelet.model import Chat, Media, Message, Press, Ref, User
from wirelet.storage import FileStorage, MemoryStorage


# --------------------------------------------------------------------------- #
# crypto
# --------------------------------------------------------------------------- #

def test_ige_matches_reference_and_round_trips(monkeypatch):
    key, iv = bytes(range(32)), bytes(range(32))
    expected = bytes.fromhex("4a7f16441cee6781e8374f261edeb88dc77147ebd5121de8d0fae7762423b6bf")
    monkeypatch.setattr(aes, "_tg", None)  # force the pure-Python path
    assert aes.ige256_encrypt(bytes(32), key, iv) == expected
    data = os.urandom(160)
    assert aes.ige256_decrypt(aes.ige256_encrypt(data, key, iv), key, iv) == data


def test_ige_matches_tgcrypto_when_installed(monkeypatch):
    tgcrypto = pytest.importorskip("tgcrypto")
    monkeypatch.setattr(aes, "_tg", None)
    for _ in range(20):
        data, key, iv = os.urandom(64), os.urandom(32), os.urandom(32)
        assert aes.ige256_encrypt(data, key, iv) == tgcrypto.ige256_encrypt(data, key, iv)


def test_pq_factorization():
    assert tlmath.split_pq(0x17ED48941A08F981) == (0x494C553B, 0x53911073)
    for p, q in [(2, 3), (1000003, 1000033), (4294967291, 4294967279)]:
        assert tlmath.split_pq(p * q) == (min(p, q), max(p, q))


def test_dh_checks():
    tlmath.check_dh_params(3, tlmath.KNOWN_DH_PRIME)
    with pytest.raises(faults.SecurityFault):
        tlmath.check_dh_params(2, tlmath.KNOWN_DH_PRIME)  # p % 8 != 7
    with pytest.raises(faults.SecurityFault):
        tlmath.check_dh_params(3, tlmath.KNOWN_DH_PRIME - 2)
    with pytest.raises(faults.SecurityFault):
        tlmath.check_dh_value(1, tlmath.KNOWN_DH_PRIME)


def _server_pack(body, auth_key, session_id, msg_id=0x5F0000000001):
    import hashlib

    data = (bytes(8) + session_id + msg_id.to_bytes(8, "little") + (1).to_bytes(4, "little")
            + len(body).to_bytes(4, "little") + body)
    data += os.urandom(12 + (-(len(data) + 12) % 16))
    msg_key = hashlib.sha256(auth_key[96:128] + data).digest()[8:24]
    key, iv = mtproto.kdf(auth_key, msg_key, outgoing=False)
    return mtproto.auth_key_id(auth_key) + msg_key + aes.ige256_encrypt(data, key, iv)


def test_unpack_verifies_everything():
    auth_key, session = os.urandom(256), os.urandom(8)
    kid = mtproto.auth_key_id(auth_key)
    packet = _server_pack(b"\x01\x02\x03\x04", auth_key, session)
    assert mtproto.unpack(packet, session, auth_key, kid).body == b"\x01\x02\x03\x04"

    tampered = packet[:-1] + bytes([packet[-1] ^ 1])
    with pytest.raises(faults.SecurityFault, match="msg_key"):
        mtproto.unpack(tampered, session, auth_key, kid)
    with pytest.raises(faults.SecurityFault, match="session_id"):
        mtproto.unpack(packet, os.urandom(8), auth_key, kid)
    with pytest.raises(faults.SecurityFault, match="auth_key_id"):
        mtproto.unpack(packet, session, auth_key, os.urandom(8))
    with pytest.raises(faults.SecurityFault, match="odd"):
        mtproto.unpack(_server_pack(b"abcd", auth_key, session, msg_id=0x5F0000000004), session, auth_key, kid)


def test_client_packets_have_valid_padding():
    auth_key = os.urandom(256)
    for size in (0, 4, 100, 1000):
        packet = mtproto.pack(bytes(size), 4, 1, 0, os.urandom(8), auth_key, mtproto.auth_key_id(auth_key))
        assert (len(packet) - 24) % 16 == 0
        assert 12 <= len(packet) - 24 - 32 - size <= 1024


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #

def test_faults_use_telegram_names():
    e = faults.from_telegram(420, "FLOOD_WAIT_17", "messages.sendMessage")
    assert isinstance(e, faults.FLOOD_WAIT) and isinstance(e, faults.TooFast)
    assert (e.amount, e.name, e.message, e.method) == (17, "FLOOD_WAIT", "FLOOD_WAIT_17", "messages.sendMessage")

    e = faults.from_telegram(303, "USER_MIGRATE_5")
    assert isinstance(e, faults.USER_MIGRATE) and isinstance(e, faults.Redirect) and e.amount == 5

    e = faults.from_telegram(400, "FILE_PART_7_MISSING")
    assert isinstance(e, faults.FILE_PART_MISSING) and e.amount == 7

    e = faults.from_telegram(400, "SOMETHING_NEW_FROM_TELEGRAM")
    assert isinstance(e, faults.SOMETHING_NEW_FROM_TELEGRAM) and isinstance(e, faults.Rejected)
    assert isinstance(faults.from_telegram(-503, "Timeout"), faults.ServerTrouble)
    assert faults.FLOOD_WAIT is faults.FLOOD_WAIT


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def _ents(entities):
    return [(type(e).__name__.removeprefix("MessageEntity"), e.offset, e.length) for e in entities]


def test_html_with_utf16_offsets():
    text, ents, mentions = parser.parse("😀 <b>bold</b> <a href='https://x.y'>link</a>", "html")
    assert text == "😀 bold link"
    # the emoji is two UTF-16 units
    assert _ents(ents) == [("Bold", 3, 4), ("TextUrl", 8, 4)]


def test_html_nested_pre_language_mentions_and_escapes():
    text, ents, mentions = parser.parse(
        '<pre><code class="language-py">x &lt; 1</code></pre>\n<a href="tg://user?id=42">Bob</a>', "html")
    assert text == "x < 1\nBob"
    assert ents[0].language == "py" and len(ents) == 1
    assert mentions == [(6, 3, 42)]


def test_markdown():
    text, ents, _ = parser.parse("**b** __i__ --u-- ~~s~~ ||sp|| `c` [l](https://e.com)", "markdown")
    assert text == "b i u s sp c l"
    assert [e[0] for e in _ents(ents)] == ["Bold", "Italic", "Underline", "Strike", "Spoiler", "Code", "TextUrl"]

    text, ents, _ = parser.parse("```python\nprint(1)\n```", "md")
    assert text == "print(1)" and ents[0].language == "python"

    text, ents, _ = parser.parse("a < b & **c**", "markdown")
    assert text == "a < b & c" and _ents(ents) == [("Bold", 8, 1)]


def test_plain_mode_leaves_text_alone():
    assert parser.parse("<b>x</b> **y**", None) == ("<b>x</b> **y**", [], [])


def test_unparse_round_trip():
    source = '😀 <b>bold <i>both</i></b> <a href="https://e.com">l</a> <code>&lt;c&gt;</code>'
    text, ents, _ = parser.parse(source, "html")
    assert parser.unparse_html(text, ents) == source


# --------------------------------------------------------------------------- #
# file ids, peers, storage
# --------------------------------------------------------------------------- #

def test_ref_round_trip():
    ref = Ref(2, 4, -123456789, 987654321, b"\x01ref", "y")
    assert Ref.unpack(ref.pack()) == ref
    with pytest.raises(ValueError):
        Ref.unpack("not-a-ref")


def test_peer_ids():
    assert utils.get_peer_id(schema.objects.PeerUser(user_id=5)) == 5
    assert utils.get_peer_id(schema.objects.PeerChat(chat_id=5)) == -5
    assert utils.get_peer_id(schema.objects.PeerChannel(channel_id=5)) == -1000000000005
    assert utils.peer_type(-1000000000005) == "channel"
    assert utils.to_raw_id(-1000000000005) == 5
    assert utils.utf16_len("a😀") == 3


async def test_file_storage_persists(tmp_path):
    s = FileStorage(tmp_path / "x.wire")
    await s.open()
    s.dc_id, s.auth_key, s.user_id, s.is_bot, s.api_id = 4, os.urandom(256), 42, True, 1
    await s.save()
    await s.update_peers([(42, 7, "bot", "MyBot", None), (-1000000000001, 9, "channel", "news", None)])
    await s.set_state(0, 10, 2, 3, 4)
    await s.close()

    again = FileStorage(tmp_path / "x.wire")
    await again.open()
    assert (again.dc_id, again.user_id, again.is_bot) == (4, 42, True)
    assert await again.get_peer_by_username("mybot") == (42, 7, "bot", "mybot", None)
    assert (await again.get_peer_by_id(-1000000000001))[1] == 9
    assert await again.get_state(0) == (10, 2, 3, 4)
    await again.close()


def test_login_key_round_trip():
    s = MemoryStorage()
    s.dc_id, s.api_id, s.auth_key, s.user_id, s.is_bot = 5, 123, os.urandom(256), 77, False
    other = MemoryStorage()
    other.import_key(s.export_key())
    assert (other.dc_id, other.api_id, other.auth_key, other.user_id, other.is_bot) == \
           (5, 123, s.auth_key, 77, False)
    with pytest.raises(ValueError):
        other.import_key("abc")


# --------------------------------------------------------------------------- #
# constraints and commands
# --------------------------------------------------------------------------- #

class FakeWire:
    me_id = 1
    me_handle = "MyBot"


def _msg(text, kind="dm", user_id=5, handle="alice", bot=False, from_me=False, media=None, reply_to=None):
    chat = Chat(id=user_id if kind in ("dm", "bot") else -100, kind=kind, handle="news" if kind == "channel" else None)
    return Message(id=1, chat=chat, text=text, from_me=from_me, media=media, reply_to=reply_to,
                   author=User(id=user_id, handle=handle, bot=bot))


async def ok(update, **constraints):
    return await Constraints(**constraints)(FakeWire(), update)


async def test_commands():
    cmd = Command(["start", "Help"])
    m = _msg('/start a "b c" d')
    assert await cmd(FakeWire(), m)
    assert (m.cmd, m.args, m.rest) == ("start", ["a", "b c", "d"], 'a "b c" d')
    assert await cmd(FakeWire(), _msg("/HELP"))
    assert await cmd(FakeWire(), _msg("/start@mybot"))
    assert not await cmd(FakeWire(), _msg("/start@otherbot"))
    assert not await cmd(FakeWire(), _msg("/started"))
    assert not await cmd(FakeWire(), _msg("start"))
    dot = Command("ping", prefix=[".", "!"])
    m = _msg(".ping\nsecond line")
    assert await dot(FakeWire(), m) and m.rest == "second line"


async def test_chat_and_author_constraints():
    group_msg = _msg("hi", kind="supergroup", user_id=9, handle="Bob")
    assert await ok(group_msg, chat="group")
    assert not await ok(group_msg, chat="dm")
    assert await ok(group_msg, chat=["channel", -100])
    assert await ok(group_msg, by="@bob") and await ok(group_msg, by=[3, 9])
    assert await ok(group_msg, by="human") and not await ok(group_msg, by="bot")
    assert await ok(_msg("x", user_id=1), by="me")
    assert await ok(_msg("x", kind="channel"), chat="@news")
    assert await ok(_msg("x", kind="bot"), chat="dm")


async def test_text_pattern_has_mine_when():
    m = _msg("order 42 and 7")
    assert await ok(m, pattern=r"\d+") and m.match.group() == "42"
    assert await ok(m, text=["nope", "order 42 and 7"]) and not await ok(m, text="order")
    assert await ok(m, has="text") and not await ok(m, has="media")
    assert await ok(m, mine=False) and not await ok(m, mine=True)

    photo = _msg("", media=Media("photo", ref="x"), reply_to=3)
    assert await ok(photo, has=["photo", "reply", "media"]) and not await ok(photo, has="voice")

    async def long_text(update):
        await asyncio.sleep(0)
        return len(update.text) > 5

    assert await ok(m, when=long_text)
    assert not await ok(m, when=[long_text, lambda u: u.text.startswith("x")])


async def test_press_data():
    press = Press(id=1, author=User(id=5), data="vote:3")
    assert await ok(press, data="vote:3") and not await ok(press, data="vote:4")
    assert await ok(press, pattern=r"vote:(\d)") and press.match.group(1) == "3"


async def test_ui_builds_layer_232_buttons():
    grid = ui.inline([ui.btn("A", "a", style="primary"), ui.copy("Copy", "xyz")], ui.url("U", "https://u.v"))
    markup = await grid.encode(FakeWire())
    first = markup.rows[0].buttons
    assert first[0].type.data == b"a" and first[0].style.bg_primary
    assert first[1].type.copy_text == "xyz"
    assert markup.rows[1].buttons[0].type.url == "https://u.v"
    assert ui.grid_from(markup).rows[0][0].data == "a"

    kb = await ui.keyboard([ui.key("Phone", contact=True)], "Cancel", once=True).encode(FakeWire())
    assert isinstance(kb.rows[0].buttons[0].type, schema.objects.ButtonTypeRequestPhone)
    assert kb.single_use and kb.rows[1].buttons[0].text == "Cancel"

    with pytest.raises(ValueError):
        await ui.inline([ui.btn("x", "y" * 65)]).encode(FakeWire())
    with pytest.raises(ValueError):
        ui.btn("x", "y", style="pink")
        await ui.inline([ui.btn("x", "y", style="pink")]).encode(FakeWire())
