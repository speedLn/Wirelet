"""MTProto 2.0 message encryption.

https://core.telegram.org/mtproto/description
"""

from __future__ import annotations

import hashlib
import os
from typing import NamedTuple

from .aes import ige256_decrypt, ige256_encrypt


class Decrypted(NamedTuple):
    salt: int
    session_id: bytes
    msg_id: int
    seq_no: int
    body: bytes


def kdf(auth_key: bytes, msg_key: bytes, outgoing: bool):
    x = 0 if outgoing else 8
    a = hashlib.sha256(msg_key + auth_key[x:x + 36]).digest()
    b = hashlib.sha256(auth_key[x + 40:x + 76] + msg_key).digest()
    key = a[0:8] + b[8:24] + a[24:32]
    iv = b[0:8] + a[8:24] + b[24:32]
    return key, iv


def auth_key_id(auth_key: bytes) -> bytes:
    return hashlib.sha1(auth_key).digest()[-8:]


def pack(
    body: bytes,
    msg_id: int,
    seq_no: int,
    salt: int,
    session_id: bytes,
    auth_key: bytes,
    key_id: bytes,
) -> bytes:
    data = (
        salt.to_bytes(8, "little", signed=True)
        + session_id
        + msg_id.to_bytes(8, "little")
        + seq_no.to_bytes(4, "little")
        + len(body).to_bytes(4, "little")
        + body
    )
    # 12..1024 random bytes of padding, total a multiple of 16.
    padding = 12 + (-(len(data) + 12) % 16) + 16 * (os.urandom(1)[0] % 4)
    data += os.urandom(padding)

    msg_key = hashlib.sha256(auth_key[88:120] + data).digest()[8:24]
    key, iv = kdf(auth_key, msg_key, outgoing=True)

    return key_id + msg_key + ige256_encrypt(data, key, iv)


def unpack(packet: bytes, session_id: bytes, auth_key: bytes, key_id: bytes) -> Decrypted:
    """Decrypt and verify one server packet. Raises ``SecurityError`` on any mismatch."""
    from wirelet.errors import SecurityError

    if len(packet) < 24 + 32 or (len(packet) - 24) % 16:
        raise SecurityError("Encrypted packet has an invalid length")
    if packet[:8] != key_id:
        raise SecurityError("auth_key_id does not match this session")

    msg_key = packet[8:24]
    key, iv = kdf(auth_key, msg_key, outgoing=False)
    data = ige256_decrypt(packet[24:], key, iv)

    # https://core.telegram.org/mtproto/security_guidelines#mtproto-encrypted-messages
    if hashlib.sha256(auth_key[96:128] + data).digest()[8:24] != msg_key:
        raise SecurityError("msg_key does not match the decrypted data")
    if data[8:16] != session_id:
        raise SecurityError("session_id does not match")

    length = int.from_bytes(data[28:32], "little")
    padding = len(data) - 32 - length
    if length % 4 or not 12 <= padding <= 1024:
        raise SecurityError("Message length or padding is invalid")

    msg_id = int.from_bytes(data[16:24], "little")
    if msg_id % 2 != 1:
        raise SecurityError("Server msg_id must be odd")

    return Decrypted(
        salt=int.from_bytes(data[0:8], "little", signed=True),
        session_id=data[8:16],
        msg_id=msg_id,
        seq_no=int.from_bytes(data[24:28], "little"),
        body=data[32:32 + length],
    )
