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


def usage_summary(app, conversation_id: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Token totals for a conversation (03 §12.1). `meta.usage` is written per step by
    the runner on `assistant`/`tool_call` events (the model-facing kinds; a `tool_call`
    row is a step just like a final `assistant` row) — never stored anywhere else, so
    totals are summed here from `meta` json on every call. `turns` groups those steps
    by the preceding `user` event, newest 20."""
    totals = app.db.execute(
        """
        SELECT
            COALESCE(SUM(json_extract(meta, '$.usage.prompt_tokens')), 0) AS prompt_tokens,
            COALESCE(SUM(json_extract(meta, '$.usage.completion_tokens')), 0) AS completion_tokens,
            COUNT(*) AS steps,
            COALESCE(SUM(CASE WHEN json_extract(meta, '$.usage') IS NOT NULL THEN 1 ELSE 0 END), 0) AS steps_with_usage
        FROM events
        WHERE conversation_id = ? AND kind IN ('assistant', 'tool_call')
        """,
        (conversation_id,),
    ).fetchone()

    rows = app.db.execute(
        "SELECT id, kind, meta, ts FROM events WHERE conversation_id=? AND kind IN ('user', 'assistant', 'tool_call') ORDER BY id ASC",
        (conversation_id,),
    ).fetchall()

    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for r in rows:
        row = db.row_to_dict(r)
        if row["kind"] == "user":
            current = {
                "user_event_id": row["id"],
                "started_at": row["ts"],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "steps": 0,
            }
            turns.append(current)
            continue
        if current is None:
            continue  # a step before any user event in this conversation (none expected)
        usage = (db.loads(row["meta"], {}) or {}).get("usage") or {}
        current["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        current["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        current["steps"] += 1

    return {
        "prompt_tokens": int(totals["prompt_tokens"] or 0),
        "completion_tokens": int(totals["completion_tokens"] or 0),
        "steps": int(totals["steps"] or 0),
        "steps_with_usage": int(totals["steps_with_usage"] or 0),
        "turns": turns[-20:],
    }


def _decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    row["meta"] = db.loads(row.get("meta"), {})
    return row


# -- conversation row helpers ---------------------------------------------------

def set_status(app, conversation_id: str, status: str, **fields: Any) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Update status (+ optional columns), announce the row, return it. The one status
    transition: everything that moves a conversation goes through here (03 §6.1).

    A conversation going back to `running` supersedes its own last report: the
    `thread_event` item that told the parent it finished/failed/stopped is dismissed
    (03 §11.3 item lifecycle). Model-posted `item_post` rows share the same source but a
    different kind, so they survive."""
    cols = ["status=?", "updated_at=?"]
    params: list[Any] = [status, db.now_iso()]
    for k, v in fields.items():
        cols.append(f"{k}=?")
        params.append(db.dumps(v) if isinstance(v, (list, dict)) else v)
    params.append(conversation_id)
    app.db.execute(f"UPDATE conversations SET {', '.join(cols)} WHERE id=?", params)
    if status == "running":
        from .. import items

        for item in items.by_source(app, f"conversation:{conversation_id}"):
            if item["kind"] == "thread_event" and item["dismissed_at"] is None:
                items.dismiss(app, item["id"])
    return announce(app, conversation_id)  # type: ignore[return-value]


def announce(app, conversation_id: str) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    """The only emitter of `conversation.updated`. One shape on the bus — the public row,
    identical to `GET /conversations/{id}` — because a surface that receives the raw row
    gets json strings where it expects lists (the web UI blanked on `pinned_notes`
    arriving as "[]", 2026-09-22)."""
    row = public_row(app, conversation_id)
    if row is not None:
        app.bus.emit("conversation.updated", row, conversation_id)
    return row


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
    row["pinned_notes"] = db.loads(row.get("pinned_notes"), [])
    return row
