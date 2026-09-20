"""Conversation primitive: inbox, events, run_turn, context assembly, compaction,
threads. Entry points every other package uses:

    post(app, conversation_id, content, source="user", meta=None) -> int
        Insert an inbox row (persist-before-act), emit `inbox.posted`, then wake().
    wake(app, conversation_id) -> None
        Schedule run_turn for the conversation unless a turn is already running.
    run_turn(app, conversation_id)   (runner.py) -- the only agent loop.

The stubs below hold the persistence half so wave-1 packages (CLI, server, approvals)
can post messages before the runner lands; the conversation package replaces wake()
with the real scheduling.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import db

log = logging.getLogger(__name__)


def post(app, conversation_id: str, content: str, source: str = "user", meta: dict[str, Any] | None = None) -> int:  # type: ignore[no-untyped-def]
    now = db.now_iso()
    cur = app.db.execute(
        "INSERT INTO inbox(conversation_id, source, content, meta, created_at) VALUES (?, ?, ?, ?, ?)",
        (conversation_id, source, content, db.dumps(meta or {}), now),
    )
    inbox_id = int(cur.lastrowid)
    app.bus.emit("inbox.posted", {"id": inbox_id, "source": source}, conversation_id)
    wake(app, conversation_id)
    return inbox_id


def wake(app, conversation_id: str) -> None:  # type: ignore[no-untyped-def]
    """Replaced by the runner package. Until then a wake only logs."""
    log.debug("wake(%s): runner not installed", conversation_id)


def install(app) -> None:  # type: ignore[no-untyped-def]
    pass
