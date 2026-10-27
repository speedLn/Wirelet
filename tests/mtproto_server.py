"""A small MTProto server that plays Telegram's side of the protocol.

It speaks the real wire format - abridged framing, the RSA_PAD + Diffie-Hellman
handshake, MTProto 2.0 encryption in the server direction, salts, containers
and rpc results - so the client is tested end to end without a network.
Requests are answered by a handler the test supplies.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional

from cryptography.hazmat.primitives.asymmetric import rsa as crsa

from wirelet import raw
from wirelet.crypto import rsa as wrsa
from wirelet.crypto.aes import ige256_decrypt, ige256_encrypt
from wirelet.crypto.math import KNOWN_DH_PRIME
from wirelet.crypto.mtproto import auth_key_id, kdf
from wirelet.raw.core import Int, Long, MsgContainer, RpcResult, TLObject

# One RSA key for every test run; generating it takes a moment.
_PRIVATE = crsa.generate_private_key(public_exponent=65537, key_size=2048)
_NUMBERS = _PRIVATE.private_numbers()
PUBLIC_KEY = wrsa.PublicKey(_NUMBERS.public_numbers.n, _NUMBERS.public_numbers.e)

G = 3
P1, P2 = 1229739323, 1402015859  # pq the client has to factor


def install_test_key(monkeypatch) -> None:
    """Make the client trust this server's RSA key in production mode."""
    monkeypatch.setattr(wrsa, "PRODUCTION_KEYS", [PUBLIC_KEY])


def _sha1(*parts: bytes) -> bytes:
    return hashlib.sha1(b"".join(parts)).digest()


def _le(v: int, n: int) -> bytes:
    return v.to_bytes(n, "little", signed=True)


class Connection:
    def __init__(self, server: "MockServer", reader, writer) -> None:
        self.server = server
        self.reader = reader
        self.writer = writer
        self.auth_key: Optional[bytes] = None
        self.session_id: Optional[bytes] = None
        self._msg_id = 0
        self._seq = 0

    # -- framing --------------------------------------------------------- #

    async def read_packet(self) -> bytes:
        words = (await self.reader.readexactly(1))[0]
        if words >= 127:
            words = int.from_bytes(await self.reader.readexactly(3), "little")
        return await self.reader.readexactly(words * 4)

    async def write_packet(self, packet: bytes) -> None:
        words = len(packet) // 4
        head = bytes([words]) if words < 127 else b"\x7f" + words.to_bytes(3, "little")
        self.writer.write(head + packet)
        await self.writer.drain()

    def next_msg_id(self, response: bool = True) -> int:
        msg_id = (int(time.time() * 2 ** 32) & ~3) | (1 if response else 3)
        if msg_id <= self._msg_id:
            msg_id = self._msg_id + 4
        self._msg_id = msg_id
        return msg_id

    # -- plain (handshake) ------------------------------------------------ #

    async def plain_reply(self, obj: TLObject) -> None:
        body = obj.write()
        await self.write_packet(bytes(8) + Long(self.next_msg_id()) + Int(len(body)) + body)

    async def handshake(self, first: bytes) -> None:
        def body_of(packet: bytes) -> TLObject:
            b = BytesIO(packet[8:])
            Long.read(b)
            length = Int.read(b)
            return TLObject.read(BytesIO(b.read(length)))

        req = body_of(first)
        assert isinstance(req, raw.functions.ReqPqMulti)
        nonce = req.nonce
        server_nonce = int.from_bytes(os.urandom(16), "little", signed=True)
        pq = (P1 * P2).to_bytes(8, "big")
        await self.plain_reply(raw.types.ResPQ(
            nonce=nonce, server_nonce=server_nonce, pq=pq,
            server_public_key_fingerprints=[12345, PUBLIC_KEY.fingerprint],
        ))

        req = body_of(await self.read_packet())
        assert isinstance(req, raw.functions.ReqDHParams)
        assert req.public_key_fingerprint == PUBLIC_KEY.fingerprint
        assert int.from_bytes(req.p, "big") * int.from_bytes(req.q, "big") == P1 * P2

        # Undo RSA_PAD.
        key_aes = pow(int.from_bytes(req.encrypted_data, "big"), _NUMBERS.d, PUBLIC_KEY.n).to_bytes(256, "big")
        temp_key = bytes(a ^ b for a, b in zip(key_aes[:32], hashlib.sha256(key_aes[32:]).digest()))
        data_with_hash = ige256_decrypt(key_aes[32:], temp_key, bytes(32))
        data_with_padding = data_with_hash[:192][::-1]
        assert hashlib.sha256(temp_key + data_with_padding).digest() == data_with_hash[192:]
        inner = TLObject.read(BytesIO(data_with_padding))
        assert isinstance(inner, raw.types.PQInnerDataDc)
        assert inner.nonce == nonce and inner.server_nonce == server_nonce
        self.server.seen_dc_ids.append(inner.dc)
        new_nonce = inner.new_nonce

        nn, sn = _le(new_nonce, 32), _le(server_nonce, 16)
        tmp_key = _sha1(nn, sn) + _sha1(sn, nn)[:12]
        tmp_iv = _sha1(sn, nn)[12:20] + _sha1(nn, nn) + nn[:4]

        a = int.from_bytes(os.urandom(256), "big")
        g_a = pow(G, a, KNOWN_DH_PRIME)
        answer = raw.types.ServerDHInnerData(
            nonce=nonce, server_nonce=server_nonce, g=G,
            dh_prime=KNOWN_DH_PRIME.to_bytes(256, "big"),
            g_a=g_a.to_bytes(256, "big"), server_time=int(time.time()),
        ).write()
        payload = _sha1(answer) + answer
        payload += os.urandom(-len(payload) % 16)
        await self.plain_reply(raw.types.ServerDHParamsOk(
            nonce=nonce, server_nonce=server_nonce,
            encrypted_answer=ige256_encrypt(payload, tmp_key, tmp_iv),
        ))

        req = body_of(await self.read_packet())
        assert isinstance(req, raw.functions.SetClientDHParams)
        decrypted = ige256_decrypt(req.encrypted_data, tmp_key, tmp_iv)
        stream = BytesIO(decrypted[20:])
        client_dh = TLObject.read(stream)
        assert _sha1(decrypted[20:20 + stream.tell()]) == decrypted[:20]
        g_b = int.from_bytes(client_dh.g_b, "big")
        auth_key = pow(g_b, a, KNOWN_DH_PRIME).to_bytes(256, "big")

        hash1 = _sha1(nn, b"\x01", _sha1(auth_key)[:8])[4:20]
        await self.plain_reply(raw.types.DhGenOk(
            nonce=nonce, server_nonce=server_nonce,
            new_nonce_hash1=int.from_bytes(hash1, "little", signed=True),
        ))
        self.server.keys[auth_key_id(auth_key)] = auth_key

    # -- encrypted -------------------------------------------------------- #

    def decrypt(self, packet: bytes):
        key = self.server.keys[packet[:8]]
        self.auth_key = key
        msg_key = packet[8:24]
        aes_key, aes_iv = kdf(key, msg_key, outgoing=True)  # client->server uses x=0
        data = ige256_decrypt(packet[24:], aes_key, aes_iv)
        assert hashlib.sha256(key[88:120] + data).digest()[8:24] == msg_key, "client msg_key wrong"
        salt = int.from_bytes(data[0:8], "little", signed=True)
        self.session_id = data[8:16]
        msg_id = int.from_bytes(data[16:24], "little")
        seq = int.from_bytes(data[24:28], "little")
        length = int.from_bytes(data[28:32], "little")
        padding = len(data) - 32 - length
        assert 12 <= padding <= 1024, f"bad client padding {padding}"
        return salt, msg_id, seq, data[32:32 + length]

    async def send(self, body: bytes, content: bool = True, response: bool = True) -> None:
        key = self.auth_key
        seq = self._seq * 2 + 1 if content else self._seq * 2
        if content:
            self._seq += 1
        data = (_le(self.server.salt, 8) + self.session_id + Long(self.next_msg_id(response))
                + Int(seq) + Int(len(body)) + body)
        data += os.urandom(12 + (-(len(data) + 12) % 16))
        msg_key = hashlib.sha256(key[96:128] + data).digest()[8:24]
        aes_key, aes_iv = kdf(key, msg_key, outgoing=False)  # server->client uses x=8
        await self.write_packet(auth_key_id(key) + msg_key + ige256_encrypt(data, aes_key, aes_iv))

    async def handle_encrypted(self, packet: bytes) -> None:
        salt, msg_id, seq, body = self.decrypt(packet)
        cid = int.from_bytes(body[:4], "little")
        if cid == MsgContainer.ID:
            for m in MsgContainer.read(BytesIO(body[4:])).messages:
                await self.handle_message(salt, m.msg_id, m.data)
        else:
            await self.handle_message(salt, msg_id, body)

    async def handle_message(self, salt: int, msg_id: int, body: bytes) -> None:
        query = TLObject.read(BytesIO(body))
        self.server.received.append(query)

        if isinstance(query, raw.types.MsgsAck):
            return

        if salt != self.server.salt:
            await self.send(raw.types.BadServerSalt(
                bad_msg_id=msg_id, bad_msg_seqno=0, error_code=48, new_server_salt=self.server.salt,
            ).write(), content=False)
            return

        if isinstance(query, (raw.functions.Ping, raw.functions.PingDelayDisconnect)):
            await self.send(raw.types.Pong(msg_id=msg_id, ping_id=query.ping_id).write(), content=False)
            return

        inner = query
        while isinstance(inner, (raw.functions.InvokeWithLayer, raw.functions.InitConnection)):
            if isinstance(inner, raw.functions.InitConnection):
                self.server.init_connections.append(inner)
            inner = inner.query

        result = await self.server.answer(inner, self)
        if result is None:
            return
        if isinstance(result, raw.types.RpcError):
            data = result.write()
        elif isinstance(result, list):
            from wirelet.raw.core import Vector
            data = Vector(result)
        elif isinstance(result, bool):
            from wirelet.raw.core import Bool
            data = Bool(result)
        else:
            data = result.write()
        await self.send(Int(RpcResult.ID, False) + Long(msg_id) + data)

    async def push(self, obj: TLObject) -> None:
        await self.send(obj.write(), response=False)

    async def run(self) -> None:
        try:
            tag = await self.reader.readexactly(1)
            assert tag == b"\xef", "client must open with the abridged tag"
            while True:
                packet = await self.read_packet()
                if packet[:8] == bytes(8):
                    await self.handshake(packet)
                else:
                    await self.handle_encrypted(packet)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if self in self.server.connections:
                self.server.connections.remove(self)
            self.writer.close()


class MockServer:
    def __init__(self, answer: Callable[[TLObject, Connection], Any]) -> None:
        self._answer = answer
        self.keys: Dict[bytes, bytes] = {}
        self.salt = int.from_bytes(os.urandom(8), "little", signed=True)
        self.connections: List[Connection] = []
        self.received: List[TLObject] = []
        self.init_connections: List[TLObject] = []
        self.seen_dc_ids: List[int] = []
        self.port = 0
        self._server = None

    async def answer(self, query, conn):
        result = self._answer(query, conn)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def start(self) -> "MockServer":
        async def on_connect(reader, writer):
            conn = Connection(self, reader, writer)
            self.connections.append(conn)
            await conn.run()

        self._server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        for c in list(self.connections):
            c.writer.close()
        self._server.close()
        await self._server.wait_closed()
