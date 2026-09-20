"""Slot [9]: the resolved-entity block for one turn (03 §7, §8.2–8.3).

The mechanics live on `Index` (`resolve` = which notes this input is about, `preload` =
how they and their first-hop links are rendered within a budget). This module is the one
call the context builder makes, so the wrapper, the exclusions and the failure behaviour
are defined in exactly one place.

The block is advisory and says so: `may_be_irrelevant="true"`. It is a cheap head start,
never a gate — the model always has `knowledge_search` (charter §2.4). It is ephemeral:
built per turn, never persisted to the transcript.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

log = logging.getLogger(__name__)

OPEN = '<knowledge may_be_irrelevant="true">'
CLOSE = "</knowledge>"


async def resolve_and_preload(
    app: Any, text: str, *, exclude: Iterable[str] = (), budget_tokens: int | None = None
) -> tuple[list[str], str]:
    """`(paths, rendered)` for this input. Anything that goes wrong yields `([], "")`:
    a retrieval fault must never cost the user a turn."""
    if not (text or "").strip() or app.index is None:
        return [], ""
    try:
        paths = await app.index.resolve(text, exclude=set(exclude or ()))
        if not paths:
            return [], ""
        return paths, app.index.preload(paths, budget_tokens)
    except Exception:
        log.exception("knowledge: resolution failed for a turn; continuing without it")
        return [], ""


async def knowledge_block(
    app: Any, text: str, *, exclude: Iterable[str] = (), budget_tokens: int | None = None
) -> str:
    """The wrapped slot-[9] text, or "" when nothing resolved."""
    _paths, rendered = await resolve_and_preload(app, text, exclude=exclude, budget_tokens=budget_tokens)
    if not rendered:
        return ""
    return f"{OPEN}\n{rendered}\n{CLOSE}"
