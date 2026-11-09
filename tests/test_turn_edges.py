"""Turn edges (2026-11-09 audit): messages that arrive at a turn's end, failed turns,
stops that race a message or the post-turn work, a thread's background processes on a
stop, and how a thread's end of turn is reported to its parent."""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from loom import conversation, processes
from loom.conversation import events, runner, stopping
from loom.models.fake import Scripted
from loom.tools import fs_guard, permissions
from loom.tools.registry import Tool, ToolResult

PY = sys.executable.replace("\\", "/")
SLEEPER = f'"{PY}" -c "import time; time.sleep(60)"'


def _events(app, cid, kinds=None):
    return events.list_events(app, cid, kinds=kinds, limit=10_000)


async def _until(pred, timeout: float = 20.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def _settle(app, cid) -> None:  # type: ignore[no-untyped-def]
    """Until no turn for `cid` is scheduled or running (re-wakes included)."""
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not runner._turn_alive(app, cid):
            await asyncio.sleep(0.02)
            if not runner._turn_alive(app, cid):
                return
    raise AssertionError("turn did not settle")


def _parent_notices(app, parent: str, child: str) -> list[str]:
    rows = app.db.execute(
        "SELECT content FROM inbox WHERE conversation_id=? AND source=? ORDER BY id", (parent, f"conversation:{child}")
    ).fetchall()
    return [r["content"] for r in rows]


@pytest.fixture(autouse=True)
async def _cleanup(app, fake):
    # A thread's report wakes the desk; whatever the desk says is not under test.
    fake.default = lambda req: Scripted(content="noted")
    yield
    for row in app.processes.list(status="running"):
        processes.kill_tree(row.get("pid"))
    for task in list(app.tasks.values()):
        if not task.done():
            task.cancel()
    await asyncio.sleep(0)


@pytest.fixture
def root(app, tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    fs_guard.add_root(app, str(path), added_by="test")
    return path


# -- messages at a turn's end ------------------------------------------------------------

async def test_a_message_sent_during_the_final_reply_is_answered_in_the_same_turn(app, fake):
    """A thread whose last reply crossed a message from its parent used to report
    "stopped without finishing" and wake again a moment later."""
    desk = app.desk_id()
    row = conversation.create_thread(app, goal="write docs", created_by="user")
    cid = row["id"]
    fake.on("status", lambda req: Scripted(content='{"state":"working","goal":"g","done":"","doing":"","next":"","blockers":""}'))
    replies = iter(["draft ready", "switched to markdown"])

    def respond(req):  # type: ignore[no-untyped-def]
        if req.conversation_id != cid:
            return Scripted(content="noted")
        text = next(replies)
        if text == "draft ready":
            conversation.post(app, cid, "use markdown instead", source=f"conversation:{desk}")
        return Scripted(content=text)

    fake.on("turn", respond)
    await _settle(app, cid)
    said = [e["content"] for e in _events(app, cid, ["assistant"])]
    assert said == ["draft ready", "switched to markdown"]
    # one turn: one report to the parent, made after the answer
    assert _parent_notices(app, desk, cid) == ["[stopped without finishing]\nswitched to markdown"]
    assert conversation.inbox.pending_count(app, cid) == 0


async def test_a_failed_turn_rewakes_for_a_message_posted_while_it_failed(app, fake):
    desk = app.desk_id()
    calls = {"n": 0}

    def respond(req):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            conversation.post(app, desk, "are you there?")
            return Scripted(outcome="fatal", error="provider down")
        return Scripted(content="yes")

    fake.on("turn", respond)
    conversation.post(app, desk, "hello")
    await _settle(app, desk)
    assert any("model call failed" in e["content"] for e in _events(app, desk, ["system"]))
    assert [e["content"] for e in _events(app, desk, ["assistant"])] == ["yes"]
    assert conversation.inbox.pending_count(app, desk) == 0


async def test_a_message_sent_right_after_a_stop_is_answered_not_swallowed(app, fake):
    desk = app.desk_id()
    started = asyncio.Event()

    async def handler(ctx, args):  # type: ignore[no-untyped-def]
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)  # a tool that takes a moment to wind down
            raise
        return ToolResult(content="finished")

    app.tools.register(Tool(name="slow", description="sleeps", parameters={"type": "object", "properties": {}}, handler=handler))
    permissions.set_policy(app, "slow", "allow")
    fake.script(Scripted(content="", tool_calls=[("slow", {})]), Scripted(content="on it: the other thing"))
    conversation.post(app, desk, "do the slow thing")
    await asyncio.wait_for(started.wait(), 20)
    await stopping.stop(app, desk, cascade=False)
    conversation.post(app, desk, "do the other thing instead")
    await _settle(app, desk)

    user = [e for e in _events(app, desk, ["user"]) if e["content"] == "do the other thing instead"]
    assert len(user) == 1
    replies = [e for e in _events(app, desk, ["assistant"]) if e["id"] > user[0]["id"]]
    assert [e["content"] for e in replies] == ["on it: the other thing"]
    # the stop rides into the next turn as harness context
    last = [c for c in fake.calls if c.conversation_id == desk][-1]
    assert any("cancelled by user" in (m.get("content") or "") for m in last.messages if m["role"] == "user")


# -- a stop during the post-turn work ----------------------------------------------------

async def test_a_stop_during_the_status_safety_net_cancels_the_turn(app, fake):
    row = conversation.create_thread(app, goal="survey", created_by="user")
    cid = row["id"]
    fake.on("turn", lambda req: Scripted(content="surveyed half" if req.conversation_id == cid else "noted"))
    fake.on("status", lambda req: Scripted(content="{}", delay_s=30))
    await _until(lambda: any(c.purpose == "status" for c in fake.calls))
    t0 = time.monotonic()
    await stopping.stop(app, cid)
    await _settle(app, cid)
    assert time.monotonic() - t0 < 10
    cancelled = [e for e in _events(app, cid, ["system"]) if e["meta"].get("cancelled")]
    assert len(cancelled) == 1 and "status update" in cancelled[0]["content"]
    assert app.conversation(cid)["status"] == "idle"


async def test_a_user_stop_records_discarded_messages(app, fake):
    """No turn runs and the person's stop throws away queued rows: the transcript says so."""
    row = conversation.create_thread(app, goal="g", created_by="user")
    cid = row["id"]
    fake.on("turn", lambda req: Scripted(content="ok"))
    fake.on("status", lambda req: Scripted(content='{"state":"working"}'))
    await _settle(app, cid)
    app.db.execute("INSERT INTO inbox(conversation_id, source, content, meta, created_at) VALUES (?, 'user', 'queued', '{}', '2026-11-09T00:00:00Z')", (cid,))
    await stopping.stop(app, cid)
    ev = [e for e in _events(app, cid, ["system"]) if e["meta"].get("stopped")]
    assert len(ev) == 1 and "1 undelivered message(s) discarded" in ev[0]["content"]
    assert ev[0]["meta"]["notice"] is True
    assert conversation.inbox.pending_count(app, cid) == 0


# -- a thread's processes on a stop ------------------------------------------------------

async def test_stopping_an_idle_thread_stops_its_processes_and_does_not_reopen_it(app, fake, root):
    desk = app.desk_id()
    row = conversation.create_thread(app, goal="serve the site", created_by="user")
    cid = row["id"]
    fake.on("turn", lambda req: Scripted(content="ok"))
    fake.on("status", lambda req: Scripted(content='{"state":"working"}'))
    await _settle(app, cid)
    proc = await app.processes.start(cid, SLEEPER, str(root), title="site")
    assert proc["status"] == "running"
    events.set_status(app, cid, "waiting_input")
    calls_before = len(fake.calls)

    stopped = await stopping.stop(app, cid)
    await _settle(app, cid)
    assert [s["id"] for s in stopped] == [cid]
    assert app.processes.get(proc["id"])["status"] == "stopped"
    assert app.conversation(cid)["status"] == "waiting_input"  # not reopened, not running
    assert len(fake.calls) == calls_before  # no turn ran
    ev = [e for e in _events(app, cid, ["system"]) if e["meta"].get("stopped")]
    assert 'background process(es) stopped: "site"' in ev[0]["content"]
    item = app.db.execute("SELECT title FROM items WHERE kind='thread_event' AND title LIKE '%stopped by the person%'").fetchone()
    assert item is not None
    assert not _parent_notices(app, desk, cid) or all("stopped by" not in n for n in _parent_notices(app, desk, cid))


async def test_the_desk_cancel_leaves_its_older_processes_running(app, fake, root):
    desk = app.desk_id()
    proc = await app.processes.start(desk, SLEEPER, str(root), title="dev server")
    assert await stopping.stop(app, desk, cascade=False) == []
    assert app.processes.get(proc["id"])["status"] == "running"


# -- how a thread's end of turn is reported ---------------------------------------------

async def test_a_thread_ending_its_turn_on_a_running_process_is_waiting(app, fake, root):
    desk = app.desk_id()
    permissions.set_policy(app, "shell", "allow")
    row = conversation.create_thread(app, goal="pull the dump", created_by="user", roots=[str(root)])
    cid = row["id"]
    fake.on("status", lambda req: Scripted(content='{"state":"waiting"}'))
    fake.script(
        Scripted(content="", tool_calls=[("shell", {"command": SLEEPER, "background": True, "title": "pull"})]),
        Scripted(content="Pull started; I continue when it exits."),
    )
    await _settle(app, cid)
    assert app.conversation(cid)["status"] == "waiting"
    assert _parent_notices(app, desk, cid) == ['[waiting on process "pull"]\nPull started; I continue when it exits.']


async def test_a_thread_out_of_steps_is_told_on_its_last_step_and_reported_as_such(app, fake):
    desk = app.desk_id()
    row = conversation.create_thread(app, goal="long job", created_by="user")
    cid = row["id"]
    app.db.execute("UPDATE conversations SET step_budget=2 WHERE id=?", (cid,))
    fake.on("status", lambda req: Scripted(content='{"state":"working"}'))
    fake.script(
        Scripted(content="", tool_calls=[("conversation_list", {})]),
        Scripted(content="Half done; next: the rest."),
    )
    await _settle(app, cid)
    last = [c for c in fake.calls if c.purpose == "turn" and c.conversation_id == cid][-1]
    assert not last.tools
    assert "last step of this turn" in last.messages[-1]["content"]
    assert _parent_notices(app, desk, cid) == ["[out of steps for this turn]\nHalf done; next: the rest."]
    assert app.conversation(cid)["status"] == "waiting_input"


async def test_the_desk_sees_its_remaining_steps(app, fake):
    desk = app.desk_id()
    fake.script(Scripted(content="hi"))
    conversation.post(app, desk, "hello")
    await _settle(app, desk)
    tail = [c for c in fake.calls if c.conversation_id == desk][0].messages[-1]["content"]
    assert "[tool calls this turn: 0 · steps remaining: 25]" in tail


async def test_a_stop_by_another_conversation_is_named_in_the_cancel_event(app, fake):
    desk = app.desk_id()
    row = conversation.create_thread(app, goal="g", created_by="user")
    cid = row["id"]
    fake.on("turn", lambda req: Scripted(content="thinking", delay_s=30) if req.conversation_id == cid else Scripted(content="noted"))
    await _until(lambda: runner._turn_alive(app, cid) and any(c.conversation_id == cid for c in fake.calls))
    await stopping.stop(app, cid, actor=f"conversation {desk}", reason="duplicate")
    await _settle(app, cid)
    cancelled = [e for e in _events(app, cid, ["system"]) if e["meta"].get("cancelled")]
    assert cancelled[0]["content"].startswith(f"stopped by conversation {desk}: duplicate")
    assert cancelled[0]["meta"]["notice"] is True
    record = [e for e in _events(app, cid, ["system"]) if e["meta"].get("stopped") is True and "actor" in e["meta"]]
    assert record and record[0]["meta"]["notice"] is False  # the turn's own event is the notice
