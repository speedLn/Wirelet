"""AES-256 in IGE mode (MTProto's message cipher) and CTR mode.

Uses ``tgcrypto`` when it is installed (``pip install wirelet[fast]``); it is a
C implementation and roughly 20x faster for file transfers. Otherwise IGE is
built on ``cryptography``'s AES block cipher.
"""

from __future__ import annotations

try:  # pragma: no cover - exercised only when the extra is installed
    import tgcrypto as _tg
except ImportError:  # pragma: no cover
    _tg = None

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BLOCK = 16


def _xor(a: bytes, b: bytes) -> bytes:
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big")


def _check(data: bytes, key: bytes, iv: bytes) -> None:
    if len(key) != 32:
        raise ValueError("AES-256 needs a 32-byte key")
    if len(iv) != 32:
        raise ValueError("IGE needs a 32-byte IV")
    if len(data) % BLOCK:
        raise ValueError("IGE data must be a multiple of 16 bytes")


def ige256_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    _check(data, key, iv)
    if _tg is not None:
        return _tg.ige256_encrypt(data, key, iv)

    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    prev_c, prev_p = iv[:BLOCK], iv[BLOCK:]
    out = bytearray()

    for i in range(0, len(data), BLOCK):
        p = data[i:i + BLOCK]
        c = _xor(enc.update(_xor(p, prev_c)), prev_p)
        out += c
        prev_c, prev_p = c, p

    return bytes(out)


def ige256_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    _check(data, key, iv)
    if _tg is not None:
        return _tg.ige256_decrypt(data, key, iv)

    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    prev_c, prev_p = iv[:BLOCK], iv[BLOCK:]
    out = bytearray()

    for i in range(0, len(data), BLOCK):
        c = data[i:i + BLOCK]
        p = _xor(dec.update(_xor(c, prev_p)), prev_c)
        out += p
        prev_c, prev_p = c, p

    return bytes(out)


class CTR256:
    """A running AES-256-CTR stream, as used by obfuscated transports."""

    def __init__(self, key: bytes, iv: bytes) -> None:
        self._ctx = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()

    def __call__(self, data: bytes) -> bytes:
        return self._ctx.update(data)


def is_accelerated() -> bool:
    return _tg is not None
