"""Conversation primitive: inbox, events, run_turn, context assembly, compaction,
threads. Entry points every other package uses:

    post(app, conversation_id, content, source="user", meta=None) -> int
        Insert an inbox row (persist-before-act), emit `inbox.posted`, then wake().
    wake(app, conversation_id) -> None
        Schedule run_turn unless a turn is already running (it claims the inbox itself).
    run_turn(app, conversation_id)   (runner.py) — the only agent loop.
    create_thread(app, ...)          (tools.py) — spawn, used by the tool and the API.
    status_fields(app, conv) / write_status(app, conv, fields, owner)   (status.py) —
        the one reader and writer of a thread's status; the sink is the scope note's
        block or, for a task without one, `status` events in its own transcript.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import db
from .runner import run_turn, wake  # noqa: F401  (re-exported)
from .status import read as status_fields  # noqa: F401
from .status import write as write_status  # noqa: F401
from .tools import ThreadError, create_thread, tools  # noqa: F401

log = logging.getLogger(__name__)


def post(app, conversation_id: str, content: str, source: str = "user", meta: dict[str, Any] | None = None) -> int:  # type: ignore[no-untyped-def]
    conv = app.conversation(conversation_id)
    if conv is None:
        raise KeyError(conversation_id)
    now = db.now_iso()
    cur = app.db.execute(
        "INSERT INTO inbox(conversation_id, source, content, meta, created_at) VALUES (?, ?, ?, ?, ?)",
        (conversation_id, source, content, db.dumps(meta or {}), now),
    )
    inbox_id = int(cur.lastrowid)
    if conv["status"] == "archived":
        # done/failed/archived conversations reopen on a new message (03 §6.1)
        app.db.execute("UPDATE conversations SET status='idle', archived_at=NULL, updated_at=? WHERE id=?", (now, conversation_id))
        app.bus.emit("conversation.updated", app.conversation(conversation_id), conversation_id)
    app.bus.emit(
        "inbox.posted",
        {"id": inbox_id, "source": source, "content": content, "created_at": now, "meta": meta or {}},
        conversation_id,
    )
    wake(app, conversation_id)
    return inbox_id


def install(app) -> None:  # type: ignore[no-untyped-def]
    for tool in tools():
        app.tools.register(tool, replace=True)


async def start(app) -> None:  # type: ignore[no-untyped-def]
    """03 §3.2 / §6.3 step 0: resume interrupted conversations. Anything left `running`
    by a crash, anything with a decided approval, and anything with undelivered inbox
    rows gets a wake; the runner's repair step sorts out the rest."""
    rows = app.db.execute(
        "SELECT DISTINCT c.id FROM conversations c "
        "LEFT JOIN inbox i ON i.conversation_id=c.id AND i.delivered_at IS NULL "
        "LEFT JOIN approvals a ON a.conversation_id=c.id AND a.status IN ('allowed','denied') "
        "WHERE c.status='running' OR i.id IS NOT NULL OR (c.status='waiting_approval' AND a.id IS NOT NULL)"
    ).fetchall()
    for r in rows:
        wake(app, r["id"])
    if rows:
        log.info("resumed %d conversation(s)", len(rows))


async def stop(app) -> None:  # type: ignore[no-untyped-def]
    return None
