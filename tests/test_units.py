import asyncio
import os

import pytest

from wirelet import errors, filters, parser, raw, utils
from wirelet.crypto import aes, math as tlmath, mtproto
from wirelet.storage import FileStorage, MemoryStorage
from wirelet.types import Chat, FileId, Message, User


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
    with pytest.raises(errors.SecurityError):
        tlmath.check_dh_params(2, tlmath.KNOWN_DH_PRIME)  # p % 8 != 7
    with pytest.raises(errors.SecurityError):
        tlmath.check_dh_params(3, tlmath.KNOWN_DH_PRIME - 2)
    with pytest.raises(errors.SecurityError):
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
    with pytest.raises(errors.SecurityError, match="msg_key"):
        mtproto.unpack(tampered, session, auth_key, kid)
    with pytest.raises(errors.SecurityError, match="session_id"):
        mtproto.unpack(packet, os.urandom(8), auth_key, kid)
    with pytest.raises(errors.SecurityError, match="auth_key_id"):
        mtproto.unpack(packet, session, auth_key, os.urandom(8))
    with pytest.raises(errors.SecurityError, match="odd"):
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

def test_errors_by_name_and_category():
    e = errors.from_rpc(420, "FLOOD_WAIT_17", "messages.sendMessage")
    assert isinstance(e, errors.FloodWait) and isinstance(e, errors.Flood)
    assert e.value == 17 and "messages.sendMessage" in str(e)

    e = errors.from_rpc(303, "USER_MIGRATE_5")
    assert isinstance(e, errors.UserMigrate) and e.value == 5

    e = errors.from_rpc(400, "SOMETHING_NEW_FROM_TELEGRAM")
    assert isinstance(e, errors.SomethingNewFromTelegram)
    assert isinstance(e, errors.BadRequest)

    assert isinstance(errors.from_rpc(-503, "Timeout"), errors.ServiceUnavailable)

    e = errors.from_rpc(400, "2FA_CONFIRM_WAIT_60")
    assert e.value == 60 and isinstance(e, errors.RPCError)


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

def test_file_id_round_trip():
    fid = FileId(2, 4, -123456789, 987654321, b"\x01ref", "y")
    assert FileId.decode(fid.encode()) == fid
    with pytest.raises(ValueError):
        FileId.decode("not-a-file-id")


def test_peer_ids():
    assert utils.get_peer_id(raw.types.PeerUser(user_id=5)) == 5
    assert utils.get_peer_id(raw.types.PeerChat(chat_id=5)) == -5
    assert utils.get_peer_id(raw.types.PeerChannel(channel_id=5)) == -1000000000005
    assert utils.peer_type(-1000000000005) == "channel"
    assert utils.to_raw_id(-1000000000005) == 5
    assert utils.utf16_len("a😀") == 3


async def test_file_storage_persists(tmp_path):
    s = FileStorage(tmp_path / "x.session")
    await s.open()
    s.dc_id, s.auth_key, s.user_id, s.is_bot, s.api_id = 4, os.urandom(256), 42, True, 1
    await s.save()
    await s.update_peers([(42, 7, "bot", "MyBot", None), (-1000000000001, 9, "channel", "news", None)])
    await s.set_state(0, 10, 2, 3, 4)
    await s.close()

    again = FileStorage(tmp_path / "x.session")
    await again.open()
    assert (again.dc_id, again.user_id, again.is_bot) == (4, 42, True)
    assert await again.get_peer_by_username("mybot") == (42, 7, "bot", "mybot", None)
    assert (await again.get_peer_by_id(-1000000000001))[1] == 9
    assert await again.get_state(0) == (10, 2, 3, 4)
    await again.close()


def test_session_string_round_trip():
    s = MemoryStorage()
    s.dc_id, s.api_id, s.auth_key, s.user_id, s.is_bot = 5, 123, os.urandom(256), 77, False
    other = MemoryStorage()
    other.import_session_string(s.export_session_string())
    assert (other.dc_id, other.api_id, other.auth_key, other.user_id, other.is_bot) == \
           (5, 123, s.auth_key, 77, False)
    with pytest.raises(ValueError):
        other.import_session_string("abc")


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #

class FakeClient:
    me_id = 1
    me_username = "MyBot"


def _message(text, chat_type="private", user_id=5, username="alice", outgoing=False):
    chat = Chat(id=user_id if chat_type == "private" else -100, type=chat_type)
    return Message(id=1, chat=chat, text=text, outgoing=outgoing,
                   from_user=User(id=user_id, username=username))


async def check(f, update):
    return await f(FakeClient(), update)


async def test_command_filter():
    f = filters.command(["start", "Help"])
    m = _message('/start a "b c"')
    assert await check(f, m) and m.command == ["start", "a", "b c"]
    assert await check(f, _message("/HELP"))
    assert await check(f, _message("/start@mybot"))
    assert not await check(f, _message("/start@otherbot"))
    assert not await check(f, _message("/started"))
    assert not await check(f, _message("start"))
    assert await check(filters.command("go", prefixes=["!", "."]), _message(".go now"))


async def test_combinators_and_chat_filters():
    m = _message("hi", chat_type="group", user_id=9, username="Bob")
    assert await check(filters.text & filters.group, m)
    assert not await check(filters.private, m)
    assert await check(~filters.private | filters.channel, m)
    assert await check(filters.user("@bob"), m)
    assert await check(filters.user(9) & filters.chat(-100), m)
    assert not await check(filters.outgoing, m)


async def test_regex_and_custom_filters():
    m = _message("order 42 and 7")
    assert await check(filters.regex(r"\d+"), m)
    assert [x.group() for x in m.matches] == ["42", "7"]

    async def is_long(flt, client, update):
        await asyncio.sleep(0)
        return len(update.text) > flt.limit

    assert await check(filters.create(is_long, limit=5), m)
    assert not await check(filters.create(is_long, limit=50), m)
