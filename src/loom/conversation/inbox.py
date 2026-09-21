"""Inbox: every input to a conversation is a row here (03 §6.2). Rows are *claimed*
(rendered into the prompt) at turn start and between steps; `delivered_at` is set only
when the turn completes cleanly, so a crashed turn redelivers.

Non-user sources render with a one-line header as user-role text:
    [from thread "3D map" 01J8…]   [schedule "weekly homework"]   [connector canvas]
"""

from __future__ import annotations

from typing import Any

from .. import db


def claim(app, conversation_id: str, after_id: int = 0) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """Undelivered rows with id > after_id, oldest first. The caller tracks the highest
    id it has claimed this turn and passes it back to avoid re-rendering.

    Each row carries `persisted_event_id`: the `user` event already written for it by an
    earlier, unfinished turn (approval pause or crash after persisting). Such rows are
    still *claimed* (they keep the turn alive and are delivered at its end) but must not
    be rendered into the transcript twice."""
    rows = app.db.execute(
        "SELECT i.*, (SELECT e.id FROM events e WHERE e.conversation_id=i.conversation_id AND e.kind='user' "
        " AND json_extract(e.meta, '$.inbox_id')=i.id ORDER BY e.id LIMIT 1) AS persisted_event_id "
        "FROM inbox i WHERE i.conversation_id=? AND i.delivered_at IS NULL AND i.id>? ORDER BY i.id",
        (conversation_id, after_id),
    ).fetchall()
    out = []
    for r in rows:
        d = db.row_to_dict(r)
        d["meta"] = db.loads(d.get("meta"), {})  # type: ignore[index]
        out.append(d)
    return out  # type: ignore[return-value]


def pending_rows(app, conversation_id: str) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """Rows posted but not yet in the transcript — what a surface shows as *queued*
    while a turn runs or waits for an approval. Same query as `claim`, minus the rows an
    unfinished turn already rendered."""
    out = []
    for r in claim(app, conversation_id):
        if r.get("persisted_event_id"):
            continue
        out.append({k: r[k] for k in ("id", "conversation_id", "source", "content", "meta", "created_at")})
    return out


def mark_delivered(app, ids: list[int]) -> None:  # type: ignore[no-untyped-def]
    if not ids:
        return
    now = db.now_iso()
    app.db.executemany("UPDATE inbox SET delivered_at=? WHERE id=?", [(now, i) for i in ids])


def pending_count(app, conversation_id: str) -> int:  # type: ignore[no-untyped-def]
    return int(
        app.db.execute(
            "SELECT COUNT(*) FROM inbox WHERE conversation_id=? AND delivered_at IS NULL", (conversation_id,)
        ).fetchone()[0]
    )


def render(app, row: dict[str, Any]) -> str:  # type: ignore[no-untyped-def]
    """User-role text for a claimed row: header for non-user sources."""
    source = row.get("source") or "user"
    content = row.get("content") or ""
    if source == "user":
        return content
    meta = row.get("meta") or {}
    if source.startswith("conversation:"):
        cid = source.split(":", 1)[1]
        title = meta.get("title")
        if not title:
            other = app.conversation(cid)
            title = (other or {}).get("title") or (other or {}).get("goal") or cid
        kind = meta.get("kind") or "thread"
        return f'[from {kind} "{title}" {cid}]\n{content}'
    if source.startswith("schedule:"):
        return f'[schedule "{meta.get("title") or source.split(":", 1)[1]}"]\n{content}'
    if source.startswith("connector:"):
        return f"[connector {source.split(':', 1)[1]}]\n{content}"
    if source == "system":
        return f"[system]\n{content}"
    return f"[{source}]\n{content}"


def is_user_source(row: dict[str, Any]) -> bool:
    return (row.get("source") or "user") == "user"
