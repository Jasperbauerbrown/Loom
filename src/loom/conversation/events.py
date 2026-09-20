"""Append-only transcript events and conversation row updates (03 §4.2, §6.1).

Event kinds: user | assistant | tool_call | tool_result | thinking | system | compaction | status.
- `tool_call.content` is the JSON list `[{"id","name","arguments"}]`; `meta` carries
  `{"text": <assistant preamble>, "reasoning_chars": n, "calls": {call_id: {policy, decision, reason}}}`.
- `tool_result.meta` carries `{"call_id", "name", "ok", "artifact_id"?}`.
- `compaction.meta` is `{"kind": "evict", "event_ids": [...]}` or
  `{"kind": "fold", "folded_through": id, "kept_from": id, "summary_chars": n}`.
Rows are never updated or deleted.
"""

from __future__ import annotations

from typing import Any

from .. import db

REPLAY_KINDS = ("user", "assistant", "tool_call", "tool_result")


def append(app, conversation_id: str, kind: str, content: str = "", meta: dict[str, Any] | None = None) -> int:  # type: ignore[no-untyped-def]
    cur = app.db.execute(
        "INSERT INTO events(conversation_id, kind, content, meta, ts) VALUES (?, ?, ?, ?, ?)",
        (conversation_id, kind, content or "", db.dumps(meta or {}), db.now_iso()),
    )
    return int(cur.lastrowid)


def get(app, event_id: int) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    return _decode(db.row_to_dict(app.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()))


def list_events(  # type: ignore[no-untyped-def]
    app,
    conversation_id: str,
    after: int = 0,
    before: int | None = None,
    limit: int = 200,
    kinds: list[str] | None = None,
    descending: bool = False,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM events WHERE conversation_id=? AND id>?"
    params: list[Any] = [conversation_id, after]
    if before is not None:
        sql += " AND id<?"
        params.append(before)
    if kinds:
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        params.extend(kinds)
    sql += " ORDER BY id %s LIMIT ?" % ("DESC" if descending else "ASC")
    params.append(limit)
    return [_decode(db.row_to_dict(r)) for r in app.db.execute(sql, params)]  # type: ignore[arg-type]


def last(app, conversation_id: str, n: int = 1, kinds: list[str] | None = None) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    rows = list_events(app, conversation_id, limit=n, kinds=kinds, descending=True)
    rows.reverse()
    return rows


def max_id(app, conversation_id: str) -> int:  # type: ignore[no-untyped-def]
    row = app.db.execute("SELECT MAX(id) FROM events WHERE conversation_id=?", (conversation_id,)).fetchone()
    return int(row[0] or 0)


def _decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    row["meta"] = db.loads(row.get("meta"), {})
    return row


# -- conversation row helpers ---------------------------------------------------

def set_status(app, conversation_id: str, status: str, **fields: Any) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Update status (+ optional columns), emit `conversation.updated`, return the row."""
    cols = ["status=?", "updated_at=?"]
    params: list[Any] = [status, db.now_iso()]
    for k, v in fields.items():
        cols.append(f"{k}=?")
        params.append(db.dumps(v) if isinstance(v, (list, dict)) else v)
    params.append(conversation_id)
    app.db.execute(f"UPDATE conversations SET {', '.join(cols)} WHERE id=?", params)
    row = public_row(app, conversation_id)
    app.bus.emit("conversation.updated", row, conversation_id)
    return row  # type: ignore[return-value]


def update(app, conversation_id: str, **fields: Any) -> None:  # type: ignore[no-untyped-def]
    """Update columns without emitting (calibration, watermarks)."""
    if not fields:
        return
    cols, params = [], []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        params.append(db.dumps(v) if isinstance(v, (list, dict)) else v)
    params.append(conversation_id)
    app.db.execute(f"UPDATE conversations SET {', '.join(cols)} WHERE id=?", params)


def public_row(app, conversation_id: str) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    row = app.conversation(conversation_id)
    if row is None:
        return None
    row["roots"] = db.loads(row.get("roots"), [])
    return row
