"""Password hashing — stdlib scrypt, no extra dependency.

Stored format: ``scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`` (urlsafe, unpadded).
scrypt is memory-hard; the params below cost ~16 MiB per hash — fine for a
self-hosted IdP at human login rates. This module is the ONLY place that knows
the scheme, so swapping in argon2 later (if the dep is added) is a one-file change.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

# n=2**14, r=8, p=1 → 128*n*r*p ≈ 16 MiB working set. maxmem set generously
# above that so OpenSSL never rejects the call on its default 32 MiB ceiling.
_N, _R, _P, _DKLEN = 1 << 14, 8, 1, 32
_MAXMEM = 64 * 1024 * 1024


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str, *, n: int = _N, r: int = _R, p: int = _P) -> str:
    if not password:
        raise ValueError("password must be non-empty")
    salt = os.urandom(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_DKLEN, maxmem=_MAXMEM
    )
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verify. Returns False on any parse/scheme/length mismatch."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = _unb64(salt_b64), _unb64(hash_b64)
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p),
            dklen=len(expected), maxmem=_MAXMEM,
        )
    except Exception:
        return False
    return hmac.compare_digest(dk, expected)
