"""SRP-2048 for two-step verification passwords.

https://core.telegram.org/api/srp
"""

from __future__ import annotations

import hashlib
import secrets

from wirelet import raw


def _h(*parts: bytes) -> bytes:
    return hashlib.sha256(b"".join(parts)).digest()


def _sh(data: bytes, salt: bytes) -> bytes:
    return _h(salt, data, salt)


def _pad(value: int) -> bytes:
    return value.to_bytes(256, "big")


def password_hash(password: str, salt1: bytes, salt2: bytes) -> bytes:
    ph1 = _sh(_sh(password.encode("utf-8"), salt1), salt2)
    return _sh(hashlib.pbkdf2_hmac("sha512", ph1, salt1, 100000), salt2)


def compute_check(
    password_info: "raw.types.account.Password", password: str
) -> "raw.types.InputCheckPasswordSRP":
    from wirelet.crypto.math import check_dh_params
    from wirelet.errors import SecurityError

    algo = password_info.current_algo
    if not isinstance(algo, raw.types.PasswordKdfAlgoSHA256SHA256PBKDF2HMACSHA512iter100000SHA256ModPow):
        raise SecurityError("Unsupported password algorithm; update wirelet")

    p = int.from_bytes(algo.p, "big")
    g = algo.g
    check_dh_params(g, p)

    g_b = int.from_bytes(password_info.srp_B, "big")
    if not 0 < g_b < p:
        raise SecurityError("Server SRP value is out of range")

    x = int.from_bytes(password_hash(password, algo.salt1, algo.salt2), "big")
    v = pow(g, x, p)
    k = int.from_bytes(_h(_pad(p), _pad(g)), "big")

    while True:
        a = int.from_bytes(secrets.token_bytes(256), "big")
        g_a = pow(g, a, p)
        if not 0 < g_a < p:
            continue
        u = int.from_bytes(_h(_pad(g_a), _pad(g_b)), "big")
        if u > 0:
            break

    t = (g_b - k * v) % p
    s_a = pow(t, a + u * x, p)
    k_a = _h(_pad(s_a))

    h_p, h_g = _h(_pad(p)), _h(_pad(g))
    m1 = _h(
        bytes(i ^ j for i, j in zip(h_p, h_g)),
        _h(algo.salt1),
        _h(algo.salt2),
        _pad(g_a),
        _pad(g_b),
        k_a,
    )

    return raw.types.InputCheckPasswordSRP(srp_id=password_info.srp_id, A=_pad(g_a), M1=m1)
