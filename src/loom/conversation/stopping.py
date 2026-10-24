"""Stopping a conversation (03 §6.4): the one codepath behind `POST /conversations/{id}/cancel`
and the `conversation_stop` tool.

A stop is the cancel signal plus whatever keeps a conversation from simply resuming:

- a **running** turn gets its signal; the runner interrupts the work in flight, writes the
  `cancelled` system event, stops its processes and ends the thread `idle`;
- a **waiting_approval** conversation has no turn to signal, so its pending approvals are
  answered `deny` — the resume the answer wakes sees the signal first, repairs the dangling
  tool call with the denied result, and ends `idle` without calling the model;
- undelivered inbox rows are discarded (a queued "please finish" must not restart what was
  just stopped; a message sent *after* the stop reopens it as usual);
- with `cascade`, every descendant thread that is not already over is stopped the same way,
  so a sub-thread that outlived an approval pause is not left working.

The stopped conversation ends `idle`, exactly as a UI cancel leaves it: not `done`/`failed`
(those mean its own `conversation_finish`), nothing reported to its parent.
"""

from __future__ import annotations

from typing import Any

from . import events, inbox

OVER = ("done", "failed", "archived")


def is_descendant(app, ancestor_id: str, conversation_id: str) -> bool:  # type: ignore[no-untyped-def]
    """True when `conversation_id` is below `ancestor_id` in the parent chain."""
    seen: set[str] = set()
    row = app.conversation(conversation_id)
    while row is not None and row.get("parent_id") and row["id"] not in seen:
        seen.add(row["id"])
        if row["parent_id"] == ancestor_id:
            return True
        row = app.conversation(row["parent_id"])
    return False


def _active(app, row: dict[str, Any]) -> bool:  # type: ignore[no-untyped-def]
    from .runner import _turn_alive

    return (
        row["status"] in ("running", "waiting_approval")
        or _turn_alive(app, row["id"])
        or inbox.pending_count(app, row["id"]) > 0
    )


def _stop_one(app, row: dict[str, Any], actor: str, reason: str) -> None:  # type: ignore[no-untyped-def]
    from ..tools import approvals
    from .runner import _turn_alive

    cid = row["id"]
    # Persist before act: the record of who stopped it and why comes first. A user cancel
    # is already recorded by the runner's own `cancelled by user` event.
    discarded = inbox.pending_count(app, cid)
    if actor != "user":
        text = f"stopped by {actor}" + (f": {reason}" if reason else "")
        if discarded:
            text += f" ({discarded} undelivered message(s) discarded)"
        ev = events.append(app, cid, "system", text, {"stopped": True, "actor": actor, "reason": reason, "was": row["status"]})
        app.bus.emit("event.appended", events.get(app, ev), cid)
    # Discarded before the signal, so a mid-turn claim cannot take them in between.
    stale = [r["id"] for r in app.db.execute("SELECT id FROM inbox WHERE conversation_id=? AND delivered_at IS NULL", (cid,)).fetchall()]
    inbox.mark_delivered(app, stale)
    pending = approvals.pending(app, cid)
    resumable = [a for a in pending if a["tool"] != approvals.INTERNAL_TOOL]
    if row["status"] == "running" or _turn_alive(app, cid) or resumable:
        app.cancel_flags.add(cid)  # cleared when the turn that sees it ends
    for a in pending:
        try:
            approvals.decide(app, a["id"], "deny")
        except approvals.AlreadyDecided:
            pass
    fresh = app.conversation(cid)
    if fresh is not None and fresh["status"] == "waiting_approval" and not resumable:
        events.set_status(app, cid, "idle")


def stop(app, conversation_id: str, *, actor: str = "user", reason: str = "", cascade: bool = True) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """Stop the conversation and (with `cascade`) its live descendants. Returns
    `[{id, title, was}]` for each one that had anything to stop; a conversation that is
    already over, or idle with nothing queued, is not in the list."""
    reason = (reason or "").strip()
    stopped: list[dict[str, Any]] = []

    def walk(cid: str) -> None:
        row = app.conversation(cid)
        if row is None or row["status"] in OVER:
            return
        if _active(app, row):
            stopped.append({"id": cid, "title": row.get("title") or row["kind"], "was": row["status"]})
            _stop_one(app, row, actor, reason)
        if cascade:
            # An idle thread can still have a live sub-thread, so the descent does not
            # stop at a quiescent node.
            for child in app.db.execute("SELECT id FROM conversations WHERE parent_id=? AND kind='thread' ORDER BY id", (cid,)).fetchall():
                walk(child["id"])

    walk(conversation_id)
    return stopped
