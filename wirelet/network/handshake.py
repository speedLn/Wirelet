"""Creating an authorization key with a data center.

Follows https://core.telegram.org/mtproto/auth_key step by step, including the
security checks from https://core.telegram.org/mtproto/security_guidelines.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from io import BytesIO
from typing import NamedTuple

from wirelet import raw
from wirelet.crypto import math as tlmath
from wirelet.crypto import rsa
from wirelet.crypto.aes import ige256_decrypt, ige256_encrypt
from wirelet.errors import AuthKeyError, SecurityError
from wirelet.raw.core import Int, Long, TLObject
from .transport import Transport

log = logging.getLogger(__name__)


class AuthKeyResult(NamedTuple):
    auth_key: bytes
    server_salt: int
    time_offset: int


def _sha1(*parts: bytes) -> bytes:
    return hashlib.sha1(b"".join(parts)).digest()


def _le(value: int, size: int) -> bytes:
    return value.to_bytes(size, "little", signed=True)


class PlainSender:
    """Sends unencrypted messages, which only the handshake uses."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self._last_msg_id = 0

    def _msg_id(self) -> int:
        msg_id = int(time.time() * 2 ** 32) & ~3
        if msg_id <= self._last_msg_id:
            msg_id = self._last_msg_id + 4
        self._last_msg_id = msg_id
        return msg_id

    async def invoke(self, query: TLObject) -> TLObject:
        body = query.write()
        packet = bytes(8) + Long(self._msg_id()) + Int(len(body)) + body
        await self.transport.send(packet)

        response = await self.transport.recv()
        b = BytesIO(response)
        if b.read(8) != bytes(8):
            raise SecurityError("Expected an unencrypted message during the handshake")
        Long.read(b)  # msg_id
        length = Int.read(b)
        return TLObject.read(BytesIO(b.read(length)))


async def create_auth_key(transport: Transport, protocol_dc_id: int, test_mode: bool) -> AuthKeyResult:
    sender = PlainSender(transport)

    last: Exception = AuthKeyError("no attempt made")
    for attempt in range(5):
        try:
            return await _exchange(sender, protocol_dc_id, test_mode)
        except (SecurityError, AuthKeyError) as e:
            log.warning("Auth key exchange attempt %d failed: %s", attempt + 1, e)
            last = e

    raise AuthKeyError(f"Could not create an auth key: {last}")


async def _exchange(sender: PlainSender, dc: int, test_mode: bool) -> AuthKeyResult:
    # 1. req_pq_multi
    nonce = int.from_bytes(os.urandom(16), "little", signed=True)
    res_pq = await sender.invoke(raw.functions.ReqPqMulti(nonce=nonce))

    if not isinstance(res_pq, raw.types.ResPQ) or res_pq.nonce != nonce:
        raise SecurityError("resPQ nonce mismatch")

    key = rsa.pick_key(res_pq.server_public_key_fingerprints, test_mode)
    if key is None:
        raise AuthKeyError("Server offered no RSA key this client knows")

    # 2. Factor pq and send our half of the key material under RSA.
    pq = int.from_bytes(res_pq.pq, "big")
    p, q = tlmath.split_pq(pq)
    p_bytes = p.to_bytes((p.bit_length() + 7) // 8, "big")
    q_bytes = q.to_bytes((q.bit_length() + 7) // 8, "big")

    new_nonce = int.from_bytes(os.urandom(32), "little", signed=True)
    server_nonce = res_pq.server_nonce

    inner = raw.types.PQInnerDataDc(
        pq=res_pq.pq, p=p_bytes, q=q_bytes, nonce=nonce,
        server_nonce=server_nonce, new_nonce=new_nonce, dc=dc,
    ).write()

    dh_params = await sender.invoke(raw.functions.ReqDHParams(
        nonce=nonce,
        server_nonce=server_nonce,
        p=p_bytes,
        q=q_bytes,
        public_key_fingerprint=key.fingerprint,
        encrypted_data=rsa.encrypt_padded(inner, key),
    ))

    if not isinstance(dh_params, raw.types.ServerDHParamsOk):
        raise AuthKeyError("Server rejected req_DH_params")
    if dh_params.nonce != nonce or dh_params.server_nonce != server_nonce:
        raise SecurityError("server_DH_params nonce mismatch")

    # 3. Decrypt the server's DH parameters with the temporary key.
    nn = _le(new_nonce, 32)
    sn = _le(server_nonce, 16)
    tmp_key = _sha1(nn, sn) + _sha1(sn, nn)[:12]
    tmp_iv = _sha1(sn, nn)[12:20] + _sha1(nn, nn) + nn[:4]

    answer_with_hash = ige256_decrypt(dh_params.encrypted_answer, tmp_key, tmp_iv)
    answer_stream = BytesIO(answer_with_hash[20:])
    server_dh = TLObject.read(answer_stream)
    answer = answer_with_hash[20:20 + answer_stream.tell()]

    if _sha1(answer) != answer_with_hash[:20]:
        raise SecurityError("server_DH_inner_data hash mismatch")
    if not isinstance(server_dh, raw.types.ServerDHInnerData):
        raise SecurityError("Expected server_DH_inner_data")
    if server_dh.nonce != nonce or server_dh.server_nonce != server_nonce:
        raise SecurityError("server_DH_inner_data nonce mismatch")

    dh_prime = int.from_bytes(server_dh.dh_prime, "big")
    g = server_dh.g
    g_a = int.from_bytes(server_dh.g_a, "big")

    tlmath.check_dh_params(g, dh_prime)
    tlmath.check_dh_value(g_a, dh_prime)

    time_offset = server_dh.server_time - int(time.time())

    # 4. Our DH half.
    retry_id = 0
    for _ in range(3):
        b = int.from_bytes(os.urandom(256), "big")
        g_b = pow(g, b, dh_prime)
        tlmath.check_dh_value(g_b, dh_prime)

        client_dh = raw.types.ClientDHInnerData(
            nonce=nonce, server_nonce=server_nonce, retry_id=retry_id,
            g_b=g_b.to_bytes(256, "big"),
        ).write()
        data = _sha1(client_dh) + client_dh
        data += os.urandom(-len(data) % 16)

        answer = await sender.invoke(raw.functions.SetClientDHParams(
            nonce=nonce, server_nonce=server_nonce,
            encrypted_data=ige256_encrypt(data, tmp_key, tmp_iv),
        ))

        if answer.nonce != nonce or answer.server_nonce != server_nonce:
            raise SecurityError("set_client_DH_params nonce mismatch")

        auth_key = pow(g_a, b, dh_prime).to_bytes(256, "big")
        aux_hash = _sha1(auth_key)[:8]

        if isinstance(answer, raw.types.DhGenOk):
            expected = _sha1(nn, b"\x01", aux_hash)[4:20]
            if _le(answer.new_nonce_hash1, 16) != expected:
                raise SecurityError("new_nonce_hash1 mismatch")

            salt = int.from_bytes(
                bytes(a ^ c for a, c in zip(nn[:8], sn[:8])), "little", signed=True
            )
            log.debug("Auth key created for DC %s", dc)
            return AuthKeyResult(auth_key, salt, time_offset)

        if isinstance(answer, raw.types.DhGenRetry):
            expected = _sha1(nn, b"\x02", aux_hash)[4:20]
            if _le(answer.new_nonce_hash2, 16) != expected:
                raise SecurityError("new_nonce_hash2 mismatch")
            retry_id = int.from_bytes(aux_hash, "little", signed=True)
            continue

        raise AuthKeyError("Server answered dh_gen_fail")

    raise AuthKeyError("Server kept asking to retry the DH exchange")
