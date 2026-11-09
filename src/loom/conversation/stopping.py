"""Stopping a conversation (03 §6.4): the one codepath behind `POST /conversations/{id}/cancel`
and the `conversation_stop` tool.

A stop is the cancel signal plus whatever keeps a conversation from simply resuming:

- a **running** turn gets its signal; the runner interrupts the work in flight, writes the
  `cancelled` system event, stops its processes and ends the thread `idle`;
- a **waiting_approval** conversation has no turn to signal, so its pending approvals are
  answered `deny` — the resume the answer wakes sees the signal first, repairs the dangling
  tool call with the denied result, and ends `idle` without calling the model;
- a **thread** also loses every background process it still has running, from any turn
  (the desk keeps its own: its cancel reaches only what the cancelled turn started) — a
  process left behind used to post its exit into the stopped thread and restart it
  (2026-11-09);
- undelivered inbox rows are discarded (a queued "please finish" must not restart what was
  just stopped; a message sent *after* the stop reopens it as usual), and the discard is
  recorded whoever stopped it;
- with `cascade`, every descendant thread that is not already over is stopped the same way,
  so a sub-thread that outlived an approval pause is not left working.

The stopped conversation ends `idle`, exactly as a UI cancel leaves it: not `done`/`failed`
(those mean its own `conversation_finish`). Its parent gets no inbox message; when the
person stopped a thread, a now-board item says so, so the desk does not go on believing
it is working (2026-11-09). The stop is replayed to the stopped conversation's model: the
runner's `cancelled` event when a turn observes the signal, else the record written here
(`meta.notice`), so a thread reopened later knows it was stopped, by whom and why.
"""

from __future__ import annotations

from typing import Any

from .. import items
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


def _processes(app, row: dict[str, Any]) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """A thread's running background processes; the desk's are never a stop's to end."""
    if row["kind"] != "thread" or getattr(app, "processes", None) is None:
        return []
    return [dict(r) for r in app.db.execute(
        "SELECT id, title FROM processes WHERE conversation_id=? AND status='running' ORDER BY id", (row["id"],)
    ).fetchall()]


def _active(app, row: dict[str, Any]) -> bool:  # type: ignore[no-untyped-def]
    from .runner import _turn_alive

    return (
        row["status"] in ("running", "waiting_approval", "waiting")
        or _turn_alive(app, row["id"])
        or inbox.pending_count(app, row["id"]) > 0
        or bool(_processes(app, row))
    )


async def _stop_one(app, row: dict[str, Any], actor: str, reason: str) -> None:  # type: ignore[no-untyped-def]
    from ..tools import approvals
    from .runner import _turn_alive

    cid = row["id"]
    discarded = inbox.pending_count(app, cid)
    procs = _processes(app, row)
    pending = approvals.pending(app, cid)
    resumable = [a for a in pending if a["tool"] != approvals.INTERNAL_TOOL]
    signalled = row["status"] == "running" or _turn_alive(app, cid) or bool(resumable)
    who = "the person" if actor == "user" else actor
    # Persist before act: the record of who stopped it, why, and what the stop throws away
    # comes first. A signalled turn writes its own `cancelled` event when it ends, so this
    # record is the model's notice only when no turn will.
    detail: list[str] = []
    if discarded:
        detail.append(f"{discarded} undelivered message(s) discarded")
    if procs:
        detail.append("background process(es) stopped: " + ", ".join(f'"{p["title"]}"' for p in procs))
    if actor != "user" or detail:
        text = f"stopped by {who}" + (f": {reason}" if reason else "") + (f" ({'; '.join(detail)})" if detail else "")
        meta = {"stopped": True, "actor": actor, "reason": reason, "was": row["status"], "notice": not signalled}
        ev = events.append(app, cid, "system", text, meta)
        app.bus.emit("event.appended", events.get(app, ev), cid)
    # Discarded before the signal, so a mid-turn claim cannot take them in between.
    stale = [r["id"] for r in app.db.execute("SELECT id FROM inbox WHERE conversation_id=? AND delivered_at IS NULL", (cid,)).fetchall()]
    inbox.mark_delivered(app, stale)
    if signalled:
        # Cleared when the turn that sees it ends; `by` names the stopper in its event.
        app.cancel_flags.add(cid, "" if actor == "user" else actor + (f": {reason}" if reason else ""))
    for a in pending:
        try:
            approvals.decide(app, a["id"], "deny")
        except approvals.AlreadyDecided:
            pass
    for p in procs:
        # Quiet: a stop message posted to the thread would reopen what is being stopped.
        try:
            await app.processes.stop(p["id"], actor=who, notify=False)
        except Exception as exc:  # one process that will not die must not keep the others alive
            ev = events.append(app, cid, "system", f'could not stop background process "{p["title"]}": {exc}')
            app.bus.emit("event.appended", events.get(app, ev), cid)
    fresh = app.conversation(cid)
    if fresh is not None and (fresh["status"] == "waiting" or (fresh["status"] == "waiting_approval" and not resumable)):
        events.set_status(app, cid, "idle")
    if actor == "user" and row["kind"] == "thread":
        title = row.get("title") or (row.get("goal") or "")[:60] or cid
        items.post(
            app, "thread_event", f'Thread "{title}" stopped by the person', reason,
            conversation_id=cid, source=f"conversation:{cid}", action={"kind": "open_conversation", "conversation_id": cid},
        )


async def stop(app, conversation_id: str, *, actor: str = "user", reason: str = "", cascade: bool = True) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """Stop the conversation and (with `cascade`) its live descendants. Returns
    `[{id, title, was}]` for each one that had anything to stop; a conversation that is
    already over, or idle with nothing queued and no process running, is not in the list."""
    reason = (reason or "").strip()
    stopped: list[dict[str, Any]] = []

    async def walk(cid: str) -> None:
        row = app.conversation(cid)
        if row is None or row["status"] in OVER:
            return
        if _active(app, row):
            stopped.append({"id": cid, "title": row.get("title") or row["kind"], "was": row["status"]})
            await _stop_one(app, row, actor, reason)
        if cascade:
            # An idle thread can still have a live sub-thread, so the descent does not
            # stop at a quiescent node.
            for child in app.db.execute("SELECT id FROM conversations WHERE parent_id=? AND kind='thread' ORDER BY id", (cid,)).fetchall():
                await walk(child["id"])

    await walk(conversation_id)
    return stopped
