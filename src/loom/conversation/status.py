"""Thread status: one reader, one writer, two sinks (03 §6.7).

A thread with a scope note keeps its status in that note's delimited block — the plan
the person can edit in place. A thread without one (a *task*) keeps it in its own
transcript as `status` events, which the model never replays and which every surface
reads through the same call. The nullable `scope_note` column picks the sink; nothing
else knows the difference, so a task that grows into a project is a field set, not a
migration (decided 2026-09-21).
"""

from __future__ import annotations

from typing import Any

from .. import db
from . import events

FIELDS = ("state", "goal", "done", "doing", "next", "blockers")
STATES = ("planning", "working", "blocked", "waiting", "done")
LINE_MAX = 200


def read(app, conv: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str]:  # type: ignore[no-untyped-def]
    """`(fields, owner, source)`. `source` names the sink: `note` for a thread with a
    scope note, `transcript` for a task, `none` for the desk or an unreadable note.
    `fields` is None when nothing has been written yet."""
    if conv.get("kind") == "desk":
        return None, None, "none"
    scope = conv.get("scope_note")
    if scope:
        try:
            fields, owner = app.vault.read_status(scope)
        except Exception:
            return None, None, "none"
        return fields, owner, "note"
    last = events.last(app, conv["id"], 1, kinds=["status"])
    if not last:
        return None, None, "transcript"
    meta = last[0].get("meta") or {}
    fields = meta.get("fields")
    if not isinstance(fields, dict):
        return None, None, "transcript"
    return fields, meta.get("owner"), "transcript"


def write(app, conv: dict[str, Any], fields: dict[str, Any], owner: str | None = None) -> str:  # type: ignore[no-untyped-def]
    """Merge `fields` into the thread's status (a None value drops the key) and return the
    sink written. The note sink never changes hands: an existing owner stays; `owner`
    only names a block that has none. The transcript sink records who wrote each event."""
    cid = conv["id"]
    scope = conv.get("scope_note")
    if scope:
        _current, existing_owner = app.vault.read_status(scope)
        app.vault.write_status(scope, existing_owner or owner or cid, fields)
        return "note"
    previous, _owner, _source = read(app, conv)
    merged: dict[str, Any] = dict(previous or {})
    for key, value in (fields or {}).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    merged["updated"] = db.now_iso()
    ev = events.append(app, cid, "status", line(merged), {"fields": merged, "owner": owner or cid})
    app.bus.emit("event.appended", events.get(app, ev), cid)
    return "transcript"


def line(fields: dict[str, Any] | None) -> str:
    """One line for lists and the status event's content: `state: working · doing: …`."""
    if not fields:
        return ""
    parts = []
    if fields.get("state"):
        parts.append(f"state: {fields['state']}")
    for key in ("doing", "next", "done", "blockers"):
        value = str(fields.get(key) or "").strip().replace("\n", " ")
        if value:
            parts.append(f"{key}: {value}")
            break
    return " · ".join(parts)[:LINE_MAX]


def render(fields: dict[str, Any] | None) -> str:
    """The fields as the model sees them in slot [5] when there is no note block."""
    if not fields:
        return "(no status yet)"
    return "\n".join(f"{key}: {value}" for key, value in fields.items() if value not in (None, ""))
