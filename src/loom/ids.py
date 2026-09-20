"""ULID generation (Crockford base32, 26 chars, time-ordered). Stdlib only."""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(ts_ms: int | None = None) -> str:
    """Return a new ULID. Monotonic within a millisecond is not guaranteed; callers that
    need strict ordering use the SQLite integer key instead."""
    ts = int(time.time() * 1000) if ts_ms is None else int(ts_ms)
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ts << 80) | rand
    out = []
    for _ in range(26):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def ulid_time_ms(value: str) -> int:
    """Extract the millisecond timestamp from a ULID."""
    ts = 0
    for ch in value[:10]:
        ts = (ts << 5) | _ALPHABET.index(ch.upper())
    return ts
