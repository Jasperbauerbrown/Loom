"""Now-board items (03 §4.2 `items`, §11.3). One writer used by every package: watcher
(setup items), MCP/connector loaders, thread events, schedule results, knowledge
rethinks, tool `item_post`.

    items.post(app, kind, title, body="", conversation_id=None, source="", action=None,
               notify=False) -> row
    items.seen(app, id) / items.dismiss(app, id) / items.unseen(app) / items.get(app, id)

`notify=True` additionally emits a `notification` event (the *interrupt* policy);
`post` alone is the *queue* policy. Rows are written before the event is emitted.
"""

from __future__ import annotations

from typing import Any

from . import db
from .ids import ulid

KINDS = ("notification", "setup", "thread_event", "schedule_result", "knowledge")


def post(
    app,  # type: ignore[no-untyped-def]
    kind: str,
    title: str,
    body: str = "",
    conversation_id: str | None = None,
    source: str = "",
    action: dict[str, Any] | None = None,
    notify: bool = False,
) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"unknown item kind {kind!r}")
    row_id = ulid()
    now = db.now_iso()
    app.db.execute(
        "INSERT INTO items(id, kind, title, body, conversation_id, source, action, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (row_id, kind, title, body, conversation_id, source, db.dumps(action or {}), now),
    )
    row = get(app, row_id)
    app.bus.emit("item.created", row, conversation_id)
    if notify:
        app.bus.emit("notification", {"title": title, "body": body, "item_id": row_id}, conversation_id)
    return row  # type: ignore[return-value]


def get(app, item_id: str) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    row = db.row_to_dict(app.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())
    if row:
        row["action"] = db.loads(row["action"], {})
    return row


def list_items(app, unseen_only: bool = False, include_dismissed: bool = False, limit: int = 200) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    where = []
    if unseen_only:
        where.append("seen_at IS NULL")
    if not include_dismissed:
        where.append("dismissed_at IS NULL")
    sql = "SELECT * FROM items"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    out = []
    for r in app.db.execute(sql, (limit,)):
        d = db.row_to_dict(r)
        d["action"] = db.loads(d["action"], {})  # type: ignore[index]
        out.append(d)
    return out  # type: ignore[return-value]


def unseen(app) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    return list_items(app, unseen_only=True)


def _mark(app, item_id: str, column: str) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    app.db.execute(f"UPDATE items SET {column}=? WHERE id=? AND {column} IS NULL", (db.now_iso(), item_id))
    row = get(app, item_id)
    if row:
        app.bus.emit("item.updated", row, row.get("conversation_id"))
    return row


def seen(app, item_id: str) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    return _mark(app, item_id, "seen_at")


def dismiss(app, item_id: str) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    return _mark(app, item_id, "dismissed_at")
