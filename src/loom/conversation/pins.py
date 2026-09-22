"""Pinned notes (03 §6.7, decided 2026-09-22): the vault notes a conversation keeps in
its context every turn — a project, a device, a procedure, anything. The column is
`conversations.pinned_notes`, a json list of vault-relative paths; this module is its
one reader and one writer.

Who pins: the thread itself through `knowledge_read(path, pin=true)` (and unpins with
`pin=false`), the spawning conversation through `conversation_create(notes=[...])`, and
the person through `PATCH /conversations/{id}` with `pinned_notes`. Context assembly
(`context.py`) renders the list in slot [5]; knowledge resolution skips pinned paths.
Loom knows nothing about what kind of note is pinned; projects are not a shape Loom
maintains.
"""

from __future__ import annotations

from typing import Any

from .. import db
from . import events


class PinError(Exception):
    pass


def get(conv: dict[str, Any]) -> list[str]:
    """The pinned paths of a conversation row (decoded or raw json)."""
    raw = conv.get("pinned_notes")
    if isinstance(raw, list):
        return [str(p) for p in raw]
    return [str(p) for p in (db.loads(raw, []) or [])]


def normalise(app, paths: list[str] | None) -> list[str]:  # type: ignore[no-untyped-def]
    """Vault-relative, deduped, in order, every one an existing note. Raises `PinError`
    naming the first missing note: a pin to nothing is a mistake to report, not to store."""
    out: list[str] = []
    seen: set[str] = set()
    for p in paths or []:
        path = app.vault.norm(str(p))
        if path in seen:
            continue
        if not app.vault.exists(path):
            raise PinError(f"no such note: {path}")
        seen.add(path)
        out.append(path)
    return out


def replace(app, conversation_id: str, paths: list[str] | None) -> list[str]:  # type: ignore[no-untyped-def]
    """Set the whole list (normalised) and announce the row. Returns the stored list."""
    pinned = normalise(app, paths)
    events.update(app, conversation_id, pinned_notes=pinned, updated_at=db.now_iso())
    row = events.public_row(app, conversation_id)
    app.bus.emit("conversation.updated", row, conversation_id)
    return pinned


def pin(app, conv: dict[str, Any], path: str) -> list[str]:  # type: ignore[no-untyped-def]
    """Add one note (idempotent). Returns the stored list."""
    current = get(conv)
    target = app.vault.norm(str(path))
    if target in current:
        return current
    return replace(app, conv["id"], [*current, target])


def unpin(app, conv: dict[str, Any], path: str) -> list[str]:  # type: ignore[no-untyped-def]
    """Remove one note (idempotent, never fails on a path that is not pinned)."""
    current = get(conv)
    target = app.vault.norm(str(path))
    remaining = [p for p in current if p != target]
    if remaining == current:
        return current
    events.update(app, conv["id"], pinned_notes=remaining, updated_at=db.now_iso())
    row = events.public_row(app, conv["id"])
    app.bus.emit("conversation.updated", row, conv["id"])
    return remaining
