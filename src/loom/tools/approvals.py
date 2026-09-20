"""Approvals: a permission question as durable, resumable state (02 §4, 03 §9.3).

When the dispatcher cannot allow a call by itself it writes an `approvals` row, marks
the conversation `waiting_approval`, emits `approval.requested`, and the turn ends. The
question survives a restart because it is a row, not a future. When the user answers,
`decide` records the answer, applies any "always" consequence, and wakes the
conversation so the runner resumes from the persisted call.

Arguments are stored exactly as the model sent them — placeholders included. Secrets are
substituted at step 5, after this point, so a value can never reach a row, an event or
the UI.

One internal action rides the same road: removing a protected path is itself an `ask`
(03 §15.3). It is recorded as a call to the pseudo-tool `protected_paths_remove`; on
`allow` this module performs the removal and does not wake anything, because no turn is
waiting on it.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import db
from ..ids import ulid
from ..models.types import ToolCall
from . import fs_guard, permissions
from .registry import ToolContext

log = logging.getLogger(__name__)

DECISIONS = ("allow", "deny", "allow_always")
INTERNAL_TOOL = "protected_paths_remove"


class NotFound(LookupError):
    """No such approval."""


class AlreadyDecided(RuntimeError):
    """The approval has already been answered."""


def _row(app: Any, approval_id: str) -> dict[str, Any] | None:
    row = db.row_to_dict(app.db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone())
    if row is not None:
        row["args"] = db.loads(row.get("args"), {}) or {}
        row["options"] = db.loads(row.get("options"), {}) or {}
    return row


def get(app: Any, approval_id: str) -> dict[str, Any] | None:
    return _row(app, approval_id)


def create(
    app: Any,
    ctx: ToolContext,
    call: ToolCall,
    reason: str,
    options: dict[str, Any] | None = None,
) -> str:
    """Persist the question before anything else happens, then tell the surfaces."""
    approval_id = ulid()
    conversation_id = ctx.conversation_id
    now = db.now_iso()
    options = dict(options or {})
    app.db.execute(
        "INSERT INTO approvals(id, conversation_id, event_id, call_id, tool, args, reason, options, "
        "status, decision, decided_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?)",
        (
            approval_id,
            conversation_id,
            int(ctx.event_id or 0),
            call.id,
            call.name,
            db.dumps(call.arguments),
            reason,
            db.dumps(options),
            now,
        ),
    )
    app.db.execute(
        "UPDATE conversations SET status='waiting_approval', updated_at=? WHERE id=?",
        (now, conversation_id),
    )
    row = _row(app, approval_id)
    app.bus.emit("approval.requested", row, conversation_id)
    conversation = app.conversation(conversation_id)
    if conversation is not None:
        app.bus.emit("conversation.updated", conversation, conversation_id)
    return approval_id


def decide(app: Any, approval_id: str, decision: str, add_root: bool = False) -> dict[str, Any]:
    """Answer an approval. `allow_always` widens exactly one rule: the root the guard
    suggested (when `add_root`), otherwise the policy row that asked."""
    if decision not in DECISIONS:
        raise ValueError(f"unknown decision {decision!r}: expected one of {list(DECISIONS)}")
    row = _row(app, approval_id)
    if row is None:
        raise NotFound(approval_id)
    if row["status"] != "pending":
        raise AlreadyDecided(f"approval {approval_id} is already {row['status']}")

    now = db.now_iso()
    status = "allowed" if decision in ("allow", "allow_always") else "denied"
    app.db.execute(
        "UPDATE approvals SET status=?, decision=?, decided_at=? WHERE id=?",
        (status, decision, now, approval_id),
    )

    options = row["options"]
    if decision == "allow_always":
        suggested = options.get("add_root")
        if suggested and add_root:
            fs_guard.add_root(app, str(suggested), added_by=f"approval:{approval_id}")
        else:
            key = options.get("policy_key") or row["tool"]
            if app.tools.get(row["tool"]) is not None or permissions.is_path_class(str(key)):
                permissions.set_policy(app, str(key), "allow")
            else:
                log.info("allow_always on %s: no policy row written (not a registered tool)", row["tool"])

    updated = _row(app, approval_id) or row
    app.bus.emit("approval.resolved", updated, row["conversation_id"])

    if row["tool"] == INTERNAL_TOOL:
        _apply_internal(app, updated, status)
    else:
        from ..conversation import wake

        wake(app, row["conversation_id"])
    return updated


def _apply_internal(app: Any, row: dict[str, Any], status: str) -> None:
    """Internal (non-model) actions that use approvals as their gate. No turn is waiting,
    so nothing is woken; the conversation goes back to idle."""
    if status == "allowed":
        path = str(row["args"].get("path") or "")
        if path:
            fs_guard.remove_protected(app, path)
    app.db.execute(
        "UPDATE conversations SET status='idle', updated_at=? WHERE id=? AND status='waiting_approval'",
        (db.now_iso(), row["conversation_id"]),
    )
    conversation = app.conversation(row["conversation_id"])
    if conversation is not None:
        app.bus.emit("conversation.updated", conversation, row["conversation_id"])


def pending(app: Any, conversation_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT id FROM approvals WHERE status='pending'"
    params: tuple[Any, ...] = ()
    if conversation_id:
        sql += " AND conversation_id=?"
        params = (conversation_id,)
    sql += " ORDER BY id"
    return [_row(app, r["id"]) for r in app.db.execute(sql, params)]  # type: ignore[misc]


def listed(app: Any, status: str | None = None, conversation_id: str | None = None) -> list[dict[str, Any]]:
    """All approvals, newest last, optionally filtered — what `GET /approvals` serves."""
    sql = "SELECT id FROM approvals WHERE 1=1"
    params: list[Any] = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if conversation_id:
        sql += " AND conversation_id=?"
        params.append(conversation_id)
    sql += " ORDER BY id"
    return [_row(app, r["id"]) for r in app.db.execute(sql, tuple(params))]  # type: ignore[misc]


def pending_for_call(app: Any, conversation_id: str, call_id: str) -> dict[str, Any] | None:
    """The question already asked for this exact call, if any — the runner uses it to
    resume without asking twice after a restart."""
    row = app.db.execute(
        "SELECT id FROM approvals WHERE conversation_id=? AND call_id=? ORDER BY id DESC LIMIT 1",
        (conversation_id, call_id),
    ).fetchone()
    return _row(app, row["id"]) if row else None
