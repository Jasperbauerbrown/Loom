"""Embeddings live on the model client (`Client.embed`, 03 §5.6); this module is the
name the rest of the codebase imports so callers never reach into `client.py` for the
vector helpers.

    from loom.models import embed
    vectors = await embed.embed(app, ["some text"])
    embed.cosine(a, b)
"""

from __future__ import annotations

import math
from array import array
from typing import Iterable, Sequence

from .client import normalise  # noqa: F401  (re-exported: the one normalisation)
from .types import PRIORITY_MAINTENANCE, Priority


async def embed(app, texts: list[str], priority: Priority = PRIORITY_MAINTENANCE) -> list[list[float]]:  # type: ignore[no-untyped-def]
    """Embed through the configured embedding role. Raises ModelError('fatal') when no
    embedding preset is configured, so callers can fall back to alias + FTS."""
    return await app.models.embed(texts, priority)


def to_blob(vector: Iterable[float]) -> bytes:
    """float32 BLOB for `notes.embedding`."""
    return array("f", [float(v) for v in vector]).tobytes()


def from_blob(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    arr = array("f")
    arr.frombytes(blob)
    return list(arr)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Dot product; vectors are normalised at write time (03 §5.6), so this is cosine.
    Falls back to a full cosine when a caller passes un-normalised input."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = math.sumprod(a, b)
    na = math.sqrt(math.sumprod(a, a))
    nb = math.sqrt(math.sumprod(b, b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    if abs(na - 1.0) < 1e-6 and abs(nb - 1.0) < 1e-6:
        return dot
    return dot / (na * nb)
