"""Cancel stops everything the turn is doing (03 §6.4, backlog 2026-09-29): the model
call in the queue or in prompt processing, a running tool call, a foreground shell's
process tree, background processes and sub-threads the turn started; the transcript and
the tool results say what was stopped."""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from loom import cancel, conversation, processes
from loom.conversation import events, runner
from loom.models.fake import Scripted
from loom.models.queue import ModelQueue
from loom.tools import fs_guard, permissions
from loom.tools.registry import Tool, ToolResult

PY = sys.executable.replace("\\", "/")


def _events(app, cid, kinds=None):
    return events.list_events(app, cid, kinds=kinds, limit=10_000)


async def _until(pred, timeout: float = 20.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def _slow_tool(app, started: asyncio.Event, name: str = "slow") -> dict:
    seen: dict = {"cancelled": False}

    async def handler(ctx, args):  # type: ignore[no-untyped-def]
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise
        return ToolResult(content="finished")

    app.tools.register(Tool(name=name, description="sleeps", parameters={"type": "object", "properties": {}}, handler=handler))
    permissions.set_policy(app, name, "allow")
    return seen


def _system_cancel(app, cid):
    rows = [e for e in _events(app, cid, ["system"]) if e["meta"].get("cancelled")]
    assert len(rows) == 1, [e["content"] for e in _events(app, cid, ["system"])]
    return rows[0]


@pytest.fixture(autouse=True)
async def _cleanup(app):
    yield
    for row in app.processes.list(status="running"):
        processes.kill_tree(row.get("pid"))
    for task in list(app.tasks.values()):
        if not task.done():
            task.cancel()
    await asyncio.sleep(0)


# -- the race itself ------------------------------------------------------------------------

async def test_race_returns_the_work_when_no_cancel():
    flags = cancel.CancelFlags()

    async def work():
        return 42

    assert await cancel.race(flags, "c", work(), "x") == 42
    assert flags.stopped("c") == []


async def test_race_signal_cancels_the_work_and_notes_it():
    flags = cancel.CancelFlags()
    ended = asyncio.Event()

    async def work():
        try:
            await asyncio.sleep(30)
        finally:
            ended.set()

    asyncio.get_running_loop().call_later(0.05, flags.add, "c")
    with pytest.raises(cancel.Cancelled) as exc:
        await cancel.race(flags, "c", work(), "model call")
    assert ended.is_set()  # awaited to its end before the race returned
    assert exc.value.value is None
    assert flags.stopped("c") == ["model call"] and "c" in flags
    flags.discard("c")
    assert "c" not in flags and flags.stopped("c") == []


async def test_race_keeps_what_the_work_answered_on_cancel():
    flags = cancel.CancelFlags()

    async def work():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            return "partial"

    asyncio.get_running_loop().call_later(0.05, flags.add, "c")
    with pytest.raises(cancel.Cancelled) as exc:
        await cancel.race(flags, "c", work(), "shell")
    assert exc.value.value == "partial"


async def test_race_with_the_signal_already_set_never_starts_the_work():
    flags = cancel.CancelFlags()
    flags.add("c")
    started = False

    async def work():
        nonlocal started
        started = True

    with pytest.raises(cancel.Cancelled):
        await cancel.race(flags, "c", work(), "x")
    assert not started and flags.stopped("c") == []


async def test_race_propagates_its_callers_cancellation_and_stops_the_work():
    flags = cancel.CancelFlags()
    ended = asyncio.Event()

    async def work():
        try:
            await asyncio.sleep(30)
        finally:
            ended.set()

    outer = asyncio.ensure_future(cancel.race(flags, "c", work(), "x"))
    await asyncio.sleep(0.02)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert ended.is_set() and flags.stopped("c") == []  # not the user's cancel: nothing noted


async def test_a_queue_wait_is_interrupted_and_gives_its_place_back():
    q = ModelQueue()
    flags = cancel.CancelFlags()
    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with q.slot("p", 1):
            holding.set()
            await release.wait()

    async def waiter():
        async with q.slot("p", 0):
            return "got it"

    h = asyncio.ensure_future(holder())
    await holding.wait()
    asyncio.get_running_loop().call_later(0.05, flags.add, "c")
    with pytest.raises(cancel.Cancelled):
        await cancel.race(flags, "c", waiter(), "model call")
    assert q.state()["depth"] == 0
    release.set()
    await h
    async with q.slot("p", 0):  # the lane is free again
        pass


# -- the turn ----------------------------------------------------------------------------

async def test_cancel_interrupts_a_model_call_with_no_delta_yet(app, fake):
    desk = app.desk_id()
    fake.script(Scripted(content="never streamed", delay_s=30))
    conversation.post(app, desk, "talk")
    task = app.tasks[f"turn:{desk}"]
    await _until(lambda: fake.calls)
    t0 = time.monotonic()
    app.cancel_flags.add(desk)
    await asyncio.wait_for(task, 5)
    assert time.monotonic() - t0 < 5
    ev = _events(app, desk, ["assistant"])
    assert ev and ev[-1]["meta"]["cancelled"] is True and ev[-1]["meta"]["finish_reason"] == "cancelled"
    sys_ev = _system_cancel(app, desk)
    assert sys_ev["content"] == "cancelled by user; stopped: model call"
    assert app.conversation(desk)["status"] == "idle"
    assert desk not in app.cancel_flags  # the signal never leaks into the next turn


async def test_cancel_interrupts_a_running_tool_and_records_it(app, fake):
    desk = app.desk_id()
    started = asyncio.Event()
    seen = _slow_tool(app, started)
    fake.script(Scripted(content="", tool_calls=[("slow", {}), ("slow", {})]))  # never answered: the turn ends at the cancel
    conversation.post(app, desk, "go")
    task = app.tasks[f"turn:{desk}"]
    await asyncio.wait_for(started.wait(), 10)
    app.cancel_flags.add(desk)
    await asyncio.wait_for(task, 5)
    assert seen["cancelled"]
    results = _events(app, desk, ["tool_result"])
    assert len(results) == 2  # the running call and the one that never started
    for r in results:
        assert r["meta"]["cancelled"] is True and r["meta"]["ok"] is False
        assert "cancelled by the user" in r["content"]
    assert len([c for c in fake.calls if c.purpose == "turn"]) == 1  # no step after the cancel
    assert _system_cancel(app, desk)["content"] == "cancelled by user; stopped: slow"
    assert _system_cancel(app, desk)["meta"]["stopped"]["calls"] == ["slow"]


async def test_cancel_kills_a_foreground_shell_and_keeps_its_output(app, fake, tmp_path):
    desk = app.desk_id()
    root = tmp_path / "work"
    root.mkdir()
    fs_guard.add_root(app, str(root), added_by="test")
    permissions.set_policy(app, "shell", "allow")
    code = "import os, time; print('child', os.getpid(), flush=True); time.sleep(60)"
    fake.script(Scripted(content="", tool_calls=[("shell", {"command": f'"{PY}" -c "{code}"', "cwd": str(root), "timeout_s": 120})]))
    conversation.post(app, desk, "run it")
    task = app.tasks[f"turn:{desk}"]
    await _until(lambda: any(e["kind"] == "tool_call" for e in _events(app, desk)))
    await asyncio.sleep(1.5)  # let the interpreter start and print
    app.cancel_flags.add(desk)
    await asyncio.wait_for(task, 20)
    res = _events(app, desk, ["tool_result"])[-1]
    assert res["meta"]["cancelled"] is True
    assert "[cancelled; process tree killed]" in res["content"]
    assert "cancelled by the user" in res["content"]
    line = next(ln for ln in res["content"].splitlines() if ln.startswith("child "))
    pid = int(line.split()[1])
    await _until(lambda: not processes.pid_alive(pid), timeout=10)
    assert _system_cancel(app, desk)["content"] == "cancelled by user; stopped: shell"


async def test_cancel_stops_background_processes_this_turn_started(app, fake, tmp_path):
    desk = app.desk_id()
    root = tmp_path / "work"
    root.mkdir()
    fs_guard.add_root(app, str(root), added_by="test")
    permissions.set_policy(app, "shell", "allow")
    sleeper = f'"{PY}" -c "import time; time.sleep(60)"'
    older = await app.processes.start(desk, sleeper, str(root), title="older")  # before the turn: not this turn's
    started = asyncio.Event()
    _slow_tool(app, started)
    fake.script(
        Scripted(content="", tool_calls=[("shell", {"command": sleeper, "cwd": str(root), "background": True, "title": "server"})]),
        Scripted(content="", tool_calls=[("slow", {})]),
    )
    conversation.post(app, desk, "start the server")
    task = app.tasks[f"turn:{desk}"]
    await asyncio.wait_for(started.wait(), 20)
    mine = [p for p in app.processes.list(conversation_id=desk) if p["title"] == "server"]
    assert mine and mine[0]["status"] == "running"
    app.cancel_flags.add(desk)
    await asyncio.wait_for(task, 20)
    assert app.processes.get(mine[0]["id"])["status"] == "stopped"
    assert app.processes.get(older["id"])["status"] == "running"
    sys_ev = _system_cancel(app, desk)
    assert sys_ev["content"] == 'cancelled by user; stopped: slow, background process "server"'
    assert [p["id"] for p in sys_ev["meta"]["stopped"]["processes"]] == [mine[0]["id"]]


async def test_cancel_cancels_sub_threads_this_turn_spawned(app, fake):
    desk = app.desk_id()
    started = asyncio.Event()
    _slow_tool(app, started)
    permissions.set_policy(app, "conversation_create", "allow")
    script = {
        desk: [
            Scripted(content="", tool_calls=[("conversation_create", {"goal": "map the house", "title": "Map"})]),
            Scripted(content="", tool_calls=[("slow", {})]),
        ],
    }

    def respond(req):  # type: ignore[no-untyped-def]
        queue = script.get(req.conversation_id)
        if queue:
            return queue.pop(0)
        return Scripted(content="thinking hard", delay_s=30)  # the sub-thread's long model call

    fake.default = respond
    conversation.post(app, desk, "spawn one")
    task = app.tasks[f"turn:{desk}"]
    await asyncio.wait_for(started.wait(), 20)
    child = app.db.execute("SELECT id FROM conversations WHERE parent_id=?", (desk,)).fetchone()["id"]
    await _until(lambda: any(c.conversation_id == child for c in fake.calls))
    child_task = app.tasks[f"turn:{child}"]
    app.cancel_flags.add(desk)
    await asyncio.wait_for(task, 10)
    await asyncio.wait_for(child_task, 10)
    sys_ev = _system_cancel(app, desk)
    assert sys_ev["content"] == 'cancelled by user; stopped: slow, sub-thread "Map"'
    assert _system_cancel(app, child)["content"] == "cancelled by user; stopped: model call"
    assert app.conversation(child)["status"] == "idle"
    # the cancelled sub-thread does not report back and re-wake the desk
    assert not [e for e in _events(app, desk, ["user"]) if (e["meta"].get("source") or "").startswith("conversation:")]


async def test_a_thread_that_already_ended_is_left_alone(app, fake):
    desk = app.desk_id()
    started = asyncio.Event()
    _slow_tool(app, started)
    permissions.set_policy(app, "conversation_create", "allow")
    script = {
        desk: [
            Scripted(content="", tool_calls=[("conversation_create", {"goal": "quick check", "title": "Quick"})]),
            Scripted(content="", tool_calls=[("slow", {})]),
        ],
    }
    def respond(req):  # type: ignore[no-untyped-def]
        queue = script.get(req.conversation_id)
        return queue.pop(0) if queue else Scripted(content="done quickly")

    fake.default = respond
    conversation.post(app, desk, "spawn one")
    task = app.tasks[f"turn:{desk}"]
    await asyncio.wait_for(started.wait(), 20)
    child = app.db.execute("SELECT id FROM conversations WHERE parent_id=?", (desk,)).fetchone()["id"]
    await _until(lambda: runner._turn_alive(app, child) is False and _events(app, child, ["assistant"]))
    app.cancel_flags.add(desk)
    await asyncio.wait_for(task, 10)
    assert _system_cancel(app, desk)["meta"]["stopped"]["threads"] == []
    assert child not in app.cancel_flags
