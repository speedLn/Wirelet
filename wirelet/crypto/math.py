"""Number theory used by the handshake: factoring pq and checking DH primes."""

from __future__ import annotations

import math
import random
import secrets

# The 2048-bit safe prime Telegram currently hands out; checking against it
# skips the slow primality tests on every handshake.
KNOWN_DH_PRIME = int(
    "c71caeb9c6b1c9048e6c522f70f13f73980d40238e3e21c14934d037563d930f"
    "48198a0aa7c14058229493d22530f4dbfa336f6e0ac925139543aed44cce7c37"
    "20fd51f69458705ac68cd4fe6b6b13abdc9746512969328454f18faf8c595f64"
    "2477fe96bb2a941d5bcd1d4ac8cc49880708fa9b378e3c4f3a9060bee67cf9a4"
    "a4a695811051907e162753b56b0f6b410dba74d8a84b2a14b3144e0ef1284754"
    "fd17ed950d5965b4b9dd46582db1178d169c6bc465b0d6ff9ca3928fef5b9ae4"
    "e418fc15e83ebea0f87fa9ff5eed70050ded2849f47bf959d956850ce929851f"
    "0d8115f635b105ee2e4e15d04b2454bf6f4fadf034b10403119cd8e3b92fcc5b",
    16,
)


def factorize(pq: int) -> int:
    """Return a non-trivial factor of ``pq`` (Pollard's rho, Brent variant)."""
    if pq % 2 == 0:
        return 2

    rng = random.Random(pq)

    while True:
        y = rng.randrange(1, pq)
        c = rng.randrange(1, pq)
        m = rng.randrange(1, pq)
        g = r = q = 1
        x = ys = 0

        while g == 1:
            x = y
            for _ in range(r):
                y = (y * y + c) % pq
            k = 0
            while k < r and g == 1:
                ys = y
                for _ in range(min(m, r - k)):
                    y = (y * y + c) % pq
                    q = q * abs(x - y) % pq
                g = math.gcd(q, pq)
                k += m
            r *= 2

        if g == pq:
            while True:
                ys = (ys * ys + c) % pq
                g = math.gcd(abs(x - ys), pq)
                if g > 1:
                    break

        if g != pq:
            return g


def split_pq(pq: int):
    p = factorize(pq)
    q = pq // p
    return (p, q) if p < q else (q, p)


def is_probable_prime(n: int, rounds: int = 40) -> bool:
    if n < 2:
        return False
    for small in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % small == 0:
            return n == small

    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1

    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def check_dh_params(g: int, p: int) -> None:
    """Raise ``SecurityError`` unless ``p`` is a safe 2048-bit prime ``g`` generates.

    https://core.telegram.org/mtproto/auth_key#dh-key-exchange-complete
    """
    from wirelet.errors import SecurityError

    if p.bit_length() != 2048:
        raise SecurityError("DH prime is not 2048 bits")

    rules = {
        2: lambda: p % 8 == 7,
        3: lambda: p % 3 == 2,
        4: lambda: True,
        5: lambda: p % 5 in (1, 4),
        6: lambda: p % 24 in (19, 23),
        7: lambda: p % 7 in (3, 5, 6),
    }
    if g not in rules or not rules[g]():
        raise SecurityError(f"g={g} does not generate the expected subgroup")

    if p != KNOWN_DH_PRIME:
        if not (is_probable_prime(p) and is_probable_prime((p - 1) // 2)):
            raise SecurityError("DH prime is not a safe prime")


def check_dh_value(value: int, p: int) -> None:
    """``1 < value < p - 1`` and away from both ends by 2^(2048-64)."""
    from wirelet.errors import SecurityError

    bound = 1 << (2048 - 64)
    if not (1 < value < p - 1) or not (bound <= value <= p - bound):
        raise SecurityError("DH value is out of the safe range")
