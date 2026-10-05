"""Approvals: a permission question as durable, resumable state (02 §4, 03 §9.3).

When the dispatcher cannot allow a call by itself it writes an `approvals` row, marks
the conversation `waiting_approval`, emits `approval.requested`, posts one notification
item so a person away from the screen is told, and the turn ends. The question survives
a restart because it is a row, not a future. When the user answers, `decide` records the
answer, applies any "always" consequence, dismisses that notification, and wakes the
conversation so the runner resumes from the persisted call.

Three answers allow, at widening scope: `allow` this once; `allow_conversation`, which
writes a `call_grants` row so this tool with exactly these arguments runs again without
asking *in this conversation only* (03 §9.3, TD audit F1); `allow_always`, which edits
the rule — the policy row that asked, or the root the guard suggested.

Arguments are stored exactly as the model sent them — placeholders included. Secrets are
substituted at step 5, after this point, so a value can never reach a row, an event or
the UI.

One internal action rides the same road: removing a protected path is itself an `ask`
(03 §15.3). It is recorded as a call to the pseudo-tool `protected_paths_remove`; on any
allowing answer this module performs the removal and does not wake anything, because no
turn is waiting on it. `allow_conversation` on it writes a grant nothing will ever read
— the dispatcher never sees this pseudo-tool — and is not special-cased for that.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import db, items
from ..ids import ulid
from ..models.types import ToolCall
from . import fs_guard, permissions
from .registry import ToolContext

log = logging.getLogger(__name__)

# The three allowing answers in widening order — once, this conversation, always — and
# the one denying answer. Anything else fails closed.
DECISIONS = ("allow", "allow_conversation", "deny", "allow_always")
INTERNAL_TOOL = "protected_paths_remove"
# `ToolContext.extras` flag: a person's click answers any ask on the spot (Apply on a
# maintenance hand-back). The dispatcher asks instead of judging; `create` surfaces nothing.
TURNLESS = "turnless_approver"


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
    from ..conversation import events  # local: `loom.conversation` imports this package

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
    if ctx.extras.get(TURNLESS):
        # The caller answers this question itself before returning (Apply on a
        # hand-back, 03 §8.6): the row is the record, but nothing is surfaced — no
        # card, no notification item, no toast, no `waiting_approval` status.
        return approval_id
    row = _row(app, approval_id)
    app.bus.emit("approval.requested", row, conversation_id)
    # A pending card in a screen nobody is looking at is a question nobody hears: one
    # notification item on the interrupt path, resolved when the answer comes (F1).
    where = str(ctx.conversation.get("title") or ctx.conversation.get("goal") or conversation_id)
    items.post(
        app,
        "notification",
        f"Approval needed: {call.name}",
        f"{where}: {reason}" if reason else where,
        conversation_id=conversation_id,
        source=f"approval:{approval_id}",
        action={"kind": "open_conversation", "conversation_id": conversation_id},
        notify=True,
    )
    events.set_status(app, conversation_id, "waiting_approval")
    return approval_id


def decide(app: Any, approval_id: str, decision: str, add_root: bool = False) -> dict[str, Any]:
    """Answer an approval. `allow_conversation` widens nothing static: it writes one
    `call_grants` row, so this tool with exactly these arguments runs again in this
    conversation and nowhere else. `allow_always` widens exactly one rule: the root the
    guard suggested (when `add_root`), otherwise the policy row that asked."""
    if decision not in DECISIONS:
        raise ValueError(f"unknown decision {decision!r}: expected one of {list(DECISIONS)}")
    row = _row(app, approval_id)
    if row is None:
        raise NotFound(approval_id)
    if row["status"] != "pending":
        raise AlreadyDecided(f"approval {approval_id} is already {row['status']}")

    now = db.now_iso()
    status = "denied" if decision == "deny" else "allowed"
    app.db.execute(
        "UPDATE approvals SET status=?, decision=?, decided_at=? WHERE id=?",
        (status, decision, now, approval_id),
    )

    options = row["options"]
    if decision == "allow_conversation":
        app.db.execute(
            "INSERT OR IGNORE INTO call_grants(conversation_id, tool, args_hash, approval_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["conversation_id"], row["tool"], permissions.args_hash(row["args"]), approval_id, now),
        )
    elif decision == "allow_always":
        suggested = options.get("add_root")
        if suggested and add_root:
            fs_guard.add_root(app, str(suggested), added_by=f"approval:{approval_id}")
        else:
            key = options.get("policy_key") or row["tool"]
            if app.tools.get(row["tool"]) is not None or permissions.is_path_class(str(key)):
                permissions.set_policy(app, str(key), "allow")
            else:
                log.info("allow_always on %s: no policy row written (not a registered tool)", row["tool"])

    # The question is answered, so its board entry is done: dismiss, not "seen" — the
    # now board hides dismissed rows and keeps seen ones.
    for item in items.by_source(app, f"approval:{approval_id}"):
        items.dismiss(app, item["id"])

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
    from ..conversation import events

    if status == "allowed":
        path = str(row["args"].get("path") or "")
        if path:
            fs_guard.remove_protected(app, path)
    cur = app.db.execute(
        "UPDATE conversations SET status='idle', updated_at=? WHERE id=? AND status='waiting_approval'",
        (db.now_iso(), row["conversation_id"]),
    )
    if cur.rowcount:
        events.announce(app, row["conversation_id"])


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
