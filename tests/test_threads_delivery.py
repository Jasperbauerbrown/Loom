"""Finish delivery carries the evidence (03 §6.7; tower-defense run audit §3).

The raylib thread finished with "confirmed by screenshot"; the walkthrough that named
the screenshot's id went to the daily log, and the desk — which could not have opened
the image anyway — spent 43 minutes re-deriving the work. `conversation_finish` now
hands the spawner the summary, the walkthrough section it just filed, and every
artifact and image the thread produced, as ids with captions.
"""

from __future__ import annotations

from loom import conversation
from loom.conversation import events, runner
from loom.conversation.tools import evidence_block, local_date, produced
from loom.models.fake import Scripted


async def _run(app, cid):
    await runner.run_turn(app, cid)
    task = app.tasks.get(f"turn:{cid}")
    if task is not None and not task.done():
        await task


async def _settle(app):
    for _ in range(40):
        pending = [t for k, t in app.tasks.items() if k.startswith("turn:") and not t.done()]
        if not pending:
            return
        await pending[0]


def _evidence(app, thread_id: str) -> tuple[str, str]:
    """(artifact_id, image_id) after giving the thread one of each."""
    art = app.artifacts.spill(thread_id, None, "PASS (0 failures)\n" + "x" * 5000)
    img = app.artifacts.store_image(thread_id, b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")
    events.append(
        app, thread_id, "tool_result",
        f"captured the game window\n[image {img['id']} attached to this result]",
        {"call_id": "c1", "name": "screenshot", "ok": True, "images": [{"id": img["id"], "mime": img["mime"]}]},
    )
    return art["id"], img["id"]


def test_produced_lists_artifacts_and_images_with_captions(app):
    row = conversation.create_thread(app, goal="verify the window renders", created_by="user")
    artifact_id, image_id = _evidence(app, row["id"])
    artifacts, images = produced(app, row["id"])
    assert artifacts == [(artifact_id, "PASS (0 failures)")]
    assert images == [(image_id, "screenshot: captured the game window")]
    # the marker the harness wrote is never mistaken for the tool's own words
    assert "attached to this result" not in images[0][1]


def test_evidence_block_carries_the_walkthrough_and_the_ids(app):
    row = conversation.create_thread(app, goal="verify the window renders", created_by="user")
    artifact_id, image_id = _evidence(app, row["id"])
    block = evidence_block(app, app.conversation(row["id"]), "log/2026-09-24.md", "Built it. Verified by screenshot.")
    assert "# Walkthrough — log/2026-09-24.md\nBuilt it. Verified by screenshot." in block
    assert f"- {artifact_id} — PASS (0 failures)" in block
    assert f"- {image_id} — screenshot: captured the game window" in block
    assert "artifact_recall" in block  # how the spawner opens them


def test_evidence_block_caps_a_long_walkthrough_visibly(app):
    row = conversation.create_thread(app, goal="write a lot", created_by="user")
    block = evidence_block(app, app.conversation(row["id"]), "log/2026-09-24.md", "y" * 20_000)
    assert len(block) < 12_000
    assert 'knowledge_read("log/2026-09-24.md") for the rest' in block


async def test_finish_delivers_summary_first_then_the_evidence(app, fake):
    """The spawner's inbox row is the summary and under it the walkthrough and the ids;
    the now-board item stays the summary alone."""
    desk = app.desk_id()
    row = conversation.create_thread(app, goal="verify the window renders", parent_id=desk, created_by="user")
    tid = row["id"]
    artifact_id, image_id = _evidence(app, tid)
    fake.script(
        Scripted(content="", tool_calls=[("conversation_finish", {
            "summary": "The window renders and a click builds a tower.",
            "walkthrough": f"Ran the game, captured the window: image {image_id}; selftest artifact {artifact_id}.",
        })]),
        Scripted(content="Read the screenshot; it holds up."),
    )
    conversation.post(app, tid, "go")
    await _run(app, tid)
    await _settle(app)

    body = app.db.execute(
        "SELECT content FROM inbox WHERE conversation_id=? AND source=? ORDER BY id DESC LIMIT 1",
        (desk, f"conversation:{tid}"),
    ).fetchone()["content"]
    assert body.startswith("[finished]\nThe window renders and a click builds a tower.")
    assert f"# Walkthrough — log/{local_date(app)}.md" in body
    assert f"- {artifact_id} — PASS (0 failures)" in body
    assert f"- {image_id} — screenshot: captured the game window" in body
    # the board stays short: the item is the summary, not the evidence
    item = app.db.execute("SELECT * FROM items WHERE kind='thread_event' ORDER BY id DESC LIMIT 1").fetchone()
    assert item["body"].strip() == "The window renders and a click builds a tower."


async def test_finish_accepts_today_in_the_walkthrough(app, fake):
    """The daily log's name is its date, so the finish writes it with the vault's
    relative-date refusal off; four finishes in one night each cost a retry over the
    word "today" (2026-09-29). Every other note keeps the refusal."""
    desk = app.desk_id()
    row = conversation.create_thread(app, goal="verify the window renders", parent_id=desk, created_by="user")
    tid = row["id"]
    fake.script(
        Scripted(content="", tool_calls=[("conversation_finish", {
            "summary": "Done.",
            "walkthrough": "Verified today by running the suite; yesterday's failure is gone.",
        })]),
    )
    conversation.post(app, tid, "go")
    await _run(app, tid)
    await _settle(app)

    note = app.vault.read(f"log/{local_date(app)}.md")
    assert "Verified today by running the suite" in note.body
    assert app.conversation(tid)["status"] == "done"
    result = app.db.execute(
        "SELECT content FROM events WHERE conversation_id=? AND kind='tool_result' ORDER BY id DESC LIMIT 1", (tid,)
    ).fetchone()["content"]
    assert "could not write the walkthrough" not in result


# -- conversation_stop (2026-10-14) --------------------------------------------------------

import asyncio
import time

from loom.conversation.tools import _stop
from loom.tools import approvals
from loom.tools.registry import ToolContext


async def _until(pred, timeout: float = 20.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def _stop_as(app, caller: str, target: str, reason: str = "duplicate"):
    ctx = ToolContext(app=app, conversation=app.conversation(caller), call_id="c")
    return _stop(ctx, {"conversation_id": target, "reason": reason})


def _thread(app, parent: str, goal: str) -> str:
    return conversation.create_thread(app, goal=goal, parent_id=parent, created_by="user")["id"]


async def _hold_in_model(app, fake, *cids):
    fake.default = lambda req: Scripted(content="thinking", delay_s=30)
    await _until(lambda: all(any(c.conversation_id == t for c in fake.calls) for t in cids))


async def test_stop_a_running_thread(app, fake):
    desk = app.desk_id()
    tid = _thread(app, desk, "map the house")
    await _hold_in_model(app, fake, tid)
    result = await _stop_as(app, desk, tid, "accidental duplicate")
    assert result.ok and tid in result.content
    await asyncio.wait_for(app.tasks[f"turn:{tid}"], 10)
    assert app.conversation(tid)["status"] == "idle"
    stops = [e for e in events.list_events(app, tid, kinds=["system"], limit=100) if e["meta"].get("actor")]
    assert len(stops) == 1 and "accidental duplicate" in stops[0]["content"]
    # a stopped thread does not report back and wake the desk
    assert not app.db.execute("SELECT 1 FROM inbox WHERE conversation_id=? AND source=?", (desk, f"conversation:{tid}")).fetchall()
    assert tid not in app.cancel_flags


async def test_stop_a_thread_waiting_on_approval(app, fake):
    desk = app.desk_id()
    fake.on("classify", lambda req: Scripted(content='{"decision":"ask","reason":"writes files"}'))
    fake.default = lambda req: Scripted(content="", tool_calls=[("shell", {"command": "echo hi > out.txt"})])
    tid = _thread(app, desk, "write a file")
    await _until(lambda: app.conversation(tid)["status"] == "waiting_approval")
    await _settle(app)
    queued = conversation.post(app, desk, "unrelated")  # desk noise must not matter
    del queued
    # a "please finish" the thread would otherwise pick up on resume
    conversation.post(app, tid, "please call conversation_finish", source=f"conversation:{desk}")
    await _settle(app)
    calls_before = len([c for c in fake.calls if c.conversation_id == tid and c.purpose == "turn"])
    result = await _stop_as(app, desk, tid, "not needed")
    assert result.ok
    await _until(lambda: app.conversation(tid)["status"] == "idle")
    await _settle(app)
    assert approvals.pending(app, tid) == []
    assert app.db.execute("SELECT status FROM approvals WHERE conversation_id=?", (tid,)).fetchone()["status"] == "denied"
    # no further model call: the denied call was repaired, the turn ended without resuming
    assert len([c for c in fake.calls if c.conversation_id == tid and c.purpose == "turn"]) == calls_before
    tr = events.list_events(app, tid, kinds=["tool_result"], limit=10)
    assert tr and not tr[-1]["meta"]["ok"]
    assert inbox_pending(app, tid) == 0
    assert app.conversation(tid)["status"] == "idle"


def inbox_pending(app, cid: str) -> int:
    from loom.conversation import inbox

    return inbox.pending_count(app, cid)


async def test_stop_refuses_a_conversation_that_is_not_a_descendant(app, fake):
    desk = app.desk_id()
    a = _thread(app, desk, "thread a")
    b = _thread(app, desk, "thread b")
    await _hold_in_model(app, fake, a, b)
    # a sibling is not a descendant, and neither is an ancestor or the desk
    for caller, target in ((a, b), (a, desk), (a, a), (desk, "nope")):
        result = await _stop_as(app, caller, target)
        assert not result.ok, (caller, target)
    assert b not in app.cancel_flags and a not in app.cancel_flags
    assert app.conversation(b)["status"] == "running"
    for t in (a, b):
        app.cancel_flags.add(t)
    await _settle(app)


async def test_stop_cascades_to_sub_threads(app, fake):
    desk = app.desk_id()
    parent = _thread(app, desk, "parent")
    await _until(lambda: app.conversation(parent) is not None)
    await _settle_quiet(app, fake)
    child = _thread(app, parent, "child")
    grandchild = _thread(app, child, "grandchild")
    await _hold_in_model(app, fake, child, grandchild)
    # the caller is the desk: grandchildren count as descendants, recursively
    result = await _stop_as(app, desk, child, "dupe tree")
    assert result.ok
    await _settle(app)
    for t in (child, grandchild):
        assert app.conversation(t)["status"] == "idle"
        assert [e for e in events.list_events(app, t, kinds=["system"], limit=100) if e["meta"].get("actor")]
    # the parent was not stopped, and a thread can stop its own sub-threads
    assert not [e for e in events.list_events(app, parent, kinds=["system"], limit=100) if e["meta"].get("actor")]
    again = await _stop_as(app, parent, grandchild)
    assert again.ok and "nothing to stop" in again.content


async def _settle_quiet(app, fake):
    fake.default = lambda req: Scripted(content="ok")
    await _settle(app)
