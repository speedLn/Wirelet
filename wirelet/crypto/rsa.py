"""Telegram's server RSA keys and the RSA_PAD scheme used during key exchange.

The keys are the ones the official clients ship with; a fingerprint is the low
64 bits of SHA-1 over the TL-serialized modulus and exponent.
"""

from __future__ import annotations

import hashlib
import os
from typing import Dict, Iterable, NamedTuple, Optional

from wirelet.raw.core import Bytes
from .aes import ige256_encrypt


class PublicKey(NamedTuple):
    n: int
    e: int

    @property
    def fingerprint(self) -> int:
        n = self.n.to_bytes((self.n.bit_length() + 7) // 8, "big")
        e = self.e.to_bytes((self.e.bit_length() + 7) // 8, "big")
        digest = hashlib.sha1(Bytes(n) + Bytes(e)).digest()
        return int.from_bytes(digest[-8:], "little", signed=True)


PRODUCTION_KEYS = [
    PublicKey(
        int(
            "e8bb3305c0b52c6cf2afdf7637313489e63e05268e5badb601af417786472e5f"
            "93b85438968e20e6729a301c0afc121bf7151f834436f7fda680847a66bf64ac"
            "cec78ee21c0b316f0edafe2f41908da7bd1f4a5107638eeb67040ace472a14f9"
            "0d9f7c2b7def99688ba3073adb5750bb02964902a359fe745d8170e36876d4fd"
            "8a5d41b2a76cbff9a13267eb9580b2d06d10357448d20d9da2191cb5d8c93982"
            "961cdfdeda629e37f1fb09a0722027696032fe61ed663db7a37f6f263d370f69"
            "db53a0dc0a1748bdaaff6209d5645485e6e001d1953255757e4b8e42813347b1"
            "1da6ab500fd0ace7e6dfa3736199ccaf9397ed0745a427dcfa6cd67bcb1acff3",
            16,
        ),
        65537,
    ),
]

TEST_KEYS = [
    PublicKey(
        int(
            "c8c11d635691fac091dd9489aedced2932aa8a0bcefef05fa800892d9b52ed03"
            "200865c9e97211cb2ee6c7ae96d3fb0e15aeffd66019b44a08a240cfdd2868a8"
            "5e1f54d6fa5deaa041f6941ddf302690d61dc476385c2fa655142353cb4e4b59"
            "f6e5b6584db76fe8b1370263246c010c93d011014113ebdf987d093f9d37c2be"
            "48352d69a1683f8f6e6c2167983c761e3ab169fde5daaa12123fa1beab621e4d"
            "a5935e9c198f82f35eae583a99386d8110ea6bd1abb0f568759f62694419ea5f"
            "69847c43462abef858b4cb5edc84e7b9226cd7bd7e183aa974a712c079dde85b"
            "9dc063b8a5c08e8f859c0ee5dcd824c7807f20153361a7f63cfd2a433a1be7f5",
            16,
        ),
        65537,
    ),
]


def keys_for(test_mode: bool) -> Dict[int, PublicKey]:
    keys = TEST_KEYS if test_mode else PRODUCTION_KEYS
    return {k.fingerprint: k for k in keys}


def pick_key(fingerprints: Iterable[int], test_mode: bool) -> Optional[PublicKey]:
    known = keys_for(test_mode)
    for fp in fingerprints:
        if fp in known:
            return known[fp]
    return None


def encrypt_padded(data: bytes, key: PublicKey) -> bytes:
    """RSA_PAD from https://core.telegram.org/mtproto/auth_key (step 4.1)."""
    if len(data) > 144:
        raise ValueError("RSA_PAD input must be at most 144 bytes")

    data_with_padding = data + os.urandom(192 - len(data))
    data_pad_reversed = data_with_padding[::-1]

    while True:
        temp_key = os.urandom(32)
        data_with_hash = data_pad_reversed + hashlib.sha256(temp_key + data_with_padding).digest()
        aes_encrypted = ige256_encrypt(data_with_hash, temp_key, bytes(32))
        aes_hash = hashlib.sha256(aes_encrypted).digest()
        temp_key_xor = bytes(a ^ b for a, b in zip(temp_key, aes_hash))
        key_aes_encrypted = temp_key_xor + aes_encrypted

        value = int.from_bytes(key_aes_encrypted, "big")
        if value < key.n:
            return pow(value, key.e, key.n).to_bytes(256, "big")
