"""Thread status: one reader, one writer, one sink (03 §6.7, decided 2026-09-22).

A thread's status lives in its own transcript as `status` events, which the model never
replays and which every surface — the now board, the status line, the thread panel —
reads through the same call. Until 2026-09-22 a thread with a scope note kept its status
in that note's delimited block instead; projects are now ordinary knowledge and threads
pin the notes they need (`pins.py`), so the note sink and its `owner=` marker are gone.
"""

from __future__ import annotations

from typing import Any

from .. import db
from . import events

FIELDS = ("state", "goal", "done", "doing", "next", "blockers")
STATES = ("planning", "working", "blocked", "waiting", "done")
LINE_MAX = 200


def read(app, conv: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str]:  # type: ignore[no-untyped-def]
    """`(fields, owner, source)`. `source` is `transcript` for a thread and `none` for the
    desk, which publishes no status. `fields` is None when nothing has been written yet."""
    if conv.get("kind") == "desk":
        return None, None, "none"
    last = events.last(app, conv["id"], 1, kinds=["status"])
    if not last:
        return None, None, "transcript"
    meta = last[0].get("meta") or {}
    fields = meta.get("fields")
    if not isinstance(fields, dict):
        return None, None, "transcript"
    return fields, meta.get("owner"), "transcript"


def write(app, conv: dict[str, Any], fields: dict[str, Any], owner: str | None = None) -> str:  # type: ignore[no-untyped-def]
    """Merge `fields` into the thread's status (a None value drops the key), append the
    `status` event and return the sink name. The event records who wrote it: the thread
    itself, the safety net on its behalf, or `user` from the thread panel."""
    cid = conv["id"]
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
    """The fields as the model sees them in slot [5]."""
    if not fields:
        return "(no status yet)"
    return "\n".join(f"{key}: {value}" for key, value in fields.items() if value not in (None, ""))
