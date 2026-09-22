"""Conversation core: run_turn end to end with the fake model (03 §6, §7)."""

from __future__ import annotations

import asyncio
import json

import pytest

from loom import conversation, db
from loom.conversation import compaction, context, events, inbox, pins, runner
from loom.conversation.tools import local_date
from loom.models.fake import Scripted
from loom.tools import approvals


def _events(app, cid, kinds=None):
    return events.list_events(app, cid, kinds=kinds, limit=10_000)


def _collect(app):
    seen = []
    app.bus.listen(lambda e: seen.append(e))
    return seen


def _ctx_block(req):
    """The harness context block (now board / goal + status, pinned notes, now line, steps)."""
    blocks = [m["content"] for m in req.messages if m["role"] == "user" and m["content"].startswith("<context")]
    assert blocks, "no harness context block in the request"
    return blocks[-1]


async def _run(app, cid):
    """Run a turn to completion, whichever task holds the lock (post() wakes one too)."""
    await runner.run_turn(app, cid)
    task = app.tasks.get(f"turn:{cid}")
    if task is not None and not task.done():
        await task


# -- plain desk turn ---------------------------------------------------------------------

async def test_desk_turn_persists_and_streams(app, fake):
    desk = app.desk_id()
    seen = _collect(app)
    fake.script(Scripted(content="Hello there.", reasoning="short think"))
    conversation.post(app, desk, "hi")
    await _run(app, desk)
    kinds = [e["kind"] for e in _events(app, desk)]
    assert kinds == ["user", "thinking", "assistant"]
    assert app.conversation(desk)["status"] == "idle"
    types = [e["type"] for e in seen]
    assert "message.start" in types and "message.delta" in types and "message.end" in types
    assert types.count("event.appended") == 3
    # inbox delivered
    assert inbox.pending_count(app, desk) == 0
    # context shape: stable system prefix first, volatile system second, user last
    req = fake.calls[0]
    assert req.messages[0]["role"] == "system" and "You are Loom" in req.messages[0]["content"]
    assert sum(1 for m in req.messages if m["role"] == "system") == 1  # chat templates want exactly one, first
    ctx_msg = _ctx_block(req)
    assert "now:" in ctx_msg and "# Now board" in ctx_msg
    assert req.messages[-2]["content"] == ctx_msg  # volatile context sits nearest the query
    assert req.messages[-1] == {"role": "user", "content": "hi"}
    assert req.tools and any(t["function"]["name"] == "fs_read" for t in req.tools)
    # calibration wrote a factor
    assert 0.5 <= app.conversation(desk)["token_factor"] <= 4.0


async def test_tool_call_round_trip(app, fake, tmp_path):
    desk = app.desk_id()
    f = app.vault_dir / "note.txt"
    f.write_text("alpha beta", encoding="utf-8")
    fake.script(
        Scripted(content="", tool_calls=[("fs_read", {"path": str(f)})]),
        Scripted(content="It says alpha beta."),
    )
    conversation.post(app, desk, "read the note")
    await _run(app, desk)
    kinds = [e["kind"] for e in _events(app, desk)]
    assert kinds == ["user", "tool_call", "tool_result", "assistant"]
    tr = _events(app, desk, ["tool_result"])[0]
    assert tr["meta"]["ok"] and "alpha beta" in tr["content"] and tr["meta"]["policy"] == "allow"
    # second call replays the tool exchange
    msgs = fake.calls[1].messages
    assert msgs[-2]["role"] == "assistant" and msgs[-2]["tool_calls"][0]["function"]["name"] == "fs_read"
    assert msgs[-1]["role"] == "tool"


# -- approvals: pause, decide, resume -----------------------------------------------------

async def test_ask_pauses_and_resumes(app, fake, monkeypatch):
    desk = app.desk_id()
    fake.on("classify", lambda req: Scripted(content='{"decision":"ask","reason":"writes files"}'))
    fake.script(
        Scripted(content="", tool_calls=[("shell", {"command": "echo hi > out.txt"})]),
        Scripted(content="done"),
    )
    conversation.post(app, desk, "make a file")
    await _run(app, desk)
    conv = app.conversation(desk)
    assert conv["status"] == "waiting_approval"
    pending = approvals.pending(app, desk)
    assert len(pending) == 1 and pending[0]["tool"] == "shell"
    assert [e["kind"] for e in _events(app, desk)] == ["user", "tool_call"]
    # the resume path must not re-ask: decide deny and run again
    approvals.decide(app, pending[0]["id"], "deny")
    await _run(app, desk)
    kinds = [e["kind"] for e in _events(app, desk)]
    assert kinds == ["user", "tool_call", "tool_result", "assistant"]
    tr = _events(app, desk, ["tool_result"])[0]
    assert not tr["meta"]["ok"] and "denied" in tr["content"]
    assert app.conversation(desk)["status"] == "idle"
    assert len([c for c in fake.calls if c.purpose == "turn"]) == 2


async def test_crash_repair_synthesises_interrupted(app, fake):
    desk = app.desk_id()
    # simulate a crash: a persisted tool_call with no result and status left `running`
    events.append(app, desk, "user", "list files")
    events.append(app, desk, "tool_call", db.dumps([{"id": "c1", "name": "fs_list", "arguments": {"path": str(app.vault_dir)}}]), {"text": ""})
    events.update(app, desk, status="running")
    fake.script(Scripted(content="recovered"))
    await _run(app, desk)
    kinds = [e["kind"] for e in _events(app, desk)]
    assert kinds == ["user", "tool_call", "tool_result", "assistant"]
    tr = _events(app, desk, ["tool_result"])[0]
    assert "interrupted" in tr["content"] and tr["meta"]["call_id"] == "c1"
    assert app.conversation(desk)["status"] == "idle"


# -- threads -----------------------------------------------------------------------------

async def _settle(app):
    """Await every turn task until no turn is pending (wakes cascade: spawn → thread → parent)."""
    for _ in range(20):
        pending = [t for n, t in app.tasks.items() if n.startswith("turn:") and not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)


async def test_thread_spawn_status_finish_notifies_parent(app, fake):
    desk = app.desk_id()
    # The project note is knowledge the desk already has; it pins it into the thread it
    # spawns (03 §6.7, decided 2026-09-22). Loom itself never writes a project note.
    app.vault.write("projects/music-bot.md", "Node Discord music bot. Run: npm start.", op="create",
                    frontmatter={"title": "Music bot", "aliases": ["music bot"], "type": "project"})
    # The whole flow is deterministic, so script it in order: the desk spawns a thread
    # (2 calls); the thread wakes automatically and runs status_update then finish
    # (2 calls); the finish event wakes the desk again (1 call).
    fake.script(
        Scripted(content="", tool_calls=[("conversation_create", {"goal": "Add play-random to the music bot", "notes": ["projects/music-bot.md"], "excerpt": "user wants random playback"})]),
        Scripted(content="Started a thread."),
        Scripted(content="", tool_calls=[("status_update", {"state": "working", "doing": "reading the code", "next": "implement"})]),
        Scripted(content="", tool_calls=[("conversation_finish", {"summary": "Added play-random.", "walkthrough": "Changed bot.js; tests pass (artifact none)."})]),
        Scripted(content="The music bot thread finished: play-random added."),
    )
    conversation.post(app, desk, "add a play-random feature to the music bot")
    await _run(app, desk)
    threads = app.db.execute("SELECT * FROM conversations WHERE kind='thread'").fetchall()
    assert len(threads) == 1
    t = dict(threads[0])
    assert t["parent_id"] == desk and t["depth"] == 1 and pins.get(t) == ["projects/music-bot.md"]
    await _settle(app)
    t2 = app.conversation(t["id"])
    assert t2["status"] == "done"
    fields, owner, source = conversation.status_fields(app, t2)
    assert source == "transcript" and fields["state"] == "done" and owner == t["id"]
    # the walkthrough is a section of the daily log, and the row remembers where
    log_path = f"log/{local_date(app)}.md"
    assert t2["walkthrough"] == log_path and f"## {t['title']} ({t['id']})" in app.vault.read(log_path).body
    assert app.vault.read("projects/music-bot.md").body.strip() == "Node Discord music bot. Run: npm start."  # untouched by the harness
    # parent got an inbox row (already consumed by its wake) + an item, and answered
    rows = app.db.execute("SELECT * FROM inbox WHERE conversation_id=? AND source=?", (desk, f"conversation:{t['id']}")).fetchall()
    assert rows and rows[0]["delivered_at"]
    items_rows = app.db.execute("SELECT * FROM items WHERE kind='thread_event'").fetchall()
    assert len(items_rows) == 1
    desk_kinds = [e["kind"] for e in _events(app, desk)]
    assert desk_kinds[-2:] == ["user", "assistant"] and "[finished]" in _events(app, desk, ["user"])[-1]["content"]
    # goal + status and the pinned note are slot [5] of the thread's context; budget line present
    thread_req = [c for c in fake.calls if c.conversation_id == t["id"] and c.purpose == "turn"][0]
    block = _ctx_block(thread_req)
    assert "# Goal\nAdd play-random to the music bot" in block and "# Status\n(no status yet)" in block
    assert "# Pinned notes\n\n## projects/music-bot.md\nNode Discord music bot." in block
    assert "[steps remaining:" in block


async def test_thread_without_status_write_gets_safety_net(app, fake):
    desk = app.desk_id()
    row = conversation.create_thread(app, goal="write docs", created_by="user")
    fake.on("status", lambda req: Scripted(content='{"state":"waiting","goal":"write docs","done":"","doing":"","next":"answer question","blockers":""}'))
    fake.script(Scripted(content="Which format do you want?"))
    await _run(app, row["id"])
    conv = app.conversation(row["id"])
    assert conv["status"] == "waiting_input"
    fields, _owner, _source = conversation.status_fields(app, conv)
    assert fields["state"] == "waiting" and fields["next"] == "answer question"
    # parent inbox has the question
    rows = app.db.execute("SELECT content FROM inbox WHERE conversation_id=? AND delivered_at IS NULL", (desk,)).fetchall()
    assert any("Which format" in r["content"] for r in rows)
    # the header says what happened, not what the thread wants (2026-09-22)
    assert any(r["content"].startswith("[stopped without finishing]\nWhich format") for r in rows)
    item = app.db.execute("SELECT title FROM items WHERE kind='thread_event'").fetchone()
    assert item["title"].endswith("stopped without finishing")


async def test_thread_only_tools_are_not_offered_to_the_desk(app, fake):
    """`Tool.kinds`: the desk called conversation_finish twice in the game test and was
    told "the desk never finishes"; it never sees the tool now, while a thread does."""
    desk = app.desk_id()
    row = conversation.create_thread(app, goal="write docs", created_by="user")
    fake.on("status", lambda req: Scripted(content='{"state":"working","goal":"write docs","done":"","doing":"","next":"","blockers":""}'))
    fake.script(Scripted(content="ok"), Scripted(content="ok"))
    conversation.post(app, desk, "hello")
    await _run(app, desk)
    await _run(app, row["id"])
    await _settle(app)

    def names(req):
        return {t["function"]["name"] for t in (req.tools or [])}

    desk_req = [c for c in fake.calls if c.conversation_id == desk and c.purpose == "turn"][-1]
    thread_req = [c for c in fake.calls if c.conversation_id == row["id"] and c.purpose == "turn"][-1]
    assert {"conversation_finish", "status_update"}.isdisjoint(names(desk_req))
    assert {"conversation_finish", "status_update"} <= names(thread_req)
    assert "conversation_create" in names(desk_req) and "conversation_create" in names(thread_req)


def test_spawn_pins_notes_and_refuses_a_missing_one(app):
    """`notes` are pinned from the first turn (deduped, vault-relative); a pin to nothing
    refuses the spawn. Sub-threads down to max_thread_depth pin whatever they need — two
    threads may pin the same note, since status no longer lives in it (2026-09-22)."""
    app.vault.write("projects/tower-game.md", "A tower defense game.", op="create", frontmatter={"type": "project"})
    app.vault.write("devices/gpu.md", "The GPU.", op="create", frontmatter={"type": "device"})
    parent = conversation.create_thread(app, goal="build the game", notes=["projects/tower-game.md", "projects/tower-game.md", "devices/gpu.md"], created_by="user")
    assert parent["pinned_notes"] == ["projects/tower-game.md", "devices/gpu.md"]
    with pytest.raises(conversation.ThreadError, match="no such note: projects/nope.md"):
        conversation.create_thread(app, goal="do the map", parent_id=parent["id"], notes=["projects/nope.md"], created_by="user")
    assert app.db.execute("SELECT COUNT(*) FROM conversations WHERE kind='thread'").fetchone()[0] == 1
    sub = conversation.create_thread(app, goal="do the map", parent_id=parent["id"], notes=["projects/tower-game.md"], created_by="user")
    assert sub["depth"] == 2 and sub["pinned_notes"] == ["projects/tower-game.md"]
    bare = conversation.create_thread(app, goal="check the tests", parent_id=parent["id"], created_by="user")
    assert bare["pinned_notes"] == [] and bare["depth"] == 2 and bare["walkthrough"] is None


# -- budgets, cancel, failure ---------------------------------------------------------------

async def test_step_budget_disables_tools_on_last_step(app, fake):
    desk = app.desk_id()
    app.settings.set(app.db, "conversation.step_budget_desk", 3)
    f = app.vault_dir / "x.txt"
    f.write_text("x", encoding="utf-8")
    call = Scripted(content="", tool_calls=[("fs_read", {"path": str(f)})])
    fake.script(call, call, call, Scripted(content="wrapping up"))
    conversation.post(app, desk, "loop")
    await _run(app, desk)
    turn_calls = [c for c in fake.calls if c.purpose == "turn"]
    assert len(turn_calls) == 3
    assert turn_calls[0].tools and turn_calls[1].tools and turn_calls[2].tools is None
    assert app.conversation(desk)["status"] == "idle"


async def test_model_fatal_is_visible_and_turn_ends(app, fake):
    desk = app.desk_id()
    fake.script(Scripted(outcome="fatal", error="model missing"))
    conversation.post(app, desk, "hi")
    await _run(app, desk)
    kinds = [e["kind"] for e in _events(app, desk)]
    assert kinds == ["user", "system"]
    assert "model missing" in _events(app, desk, ["system"])[0]["content"]
    assert app.conversation(desk)["status"] == "idle"


async def test_transient_retries_once(app, fake):
    desk = app.desk_id()
    fake.script(Scripted(outcome="transient", error="boom"), Scripted(content="ok now"))
    conversation.post(app, desk, "hi")
    await _run(app, desk)
    assert [e["kind"] for e in _events(app, desk)] == ["user", "assistant"]


async def test_cancel_persists_partial(app, fake):
    desk = app.desk_id()
    fake.script(Scripted(content="one two three four five", delay_s=0.4))
    conversation.post(app, desk, "talk")  # wakes the turn task
    task = app.tasks[f"turn:{desk}"]
    await asyncio.sleep(0.05)  # well inside the fake's delay, even under full-suite load
    app.cancel_flags.add(desk)
    await task
    ev = _events(app, desk, ["assistant"])
    assert ev and ev[0]["meta"]["cancelled"] is True
    assert app.conversation(desk)["status"] == "idle"


# -- compaction ----------------------------------------------------------------------------

async def test_compaction_evicts_then_folds(app, fake):
    desk = app.desk_id()
    conv = app.conversation(desk)
    # a long transcript of tool results
    for i in range(10):
        events.append(app, desk, "user", f"step {i}")
        events.append(app, desk, "tool_call", db.dumps([{"id": f"c{i}", "name": "fs_read", "arguments": {"path": "p"}}]), {"text": ""})
        events.append(app, desk, "tool_result", "R" * 5000, {"call_id": f"c{i}", "name": "fs_read", "ok": True})
        events.append(app, desk, "assistant", f"answer {i}")
    conv = app.conversation(desk)
    assert compaction.evict(app, conv) is True
    ctx = context.assemble(app, conv, [], None)
    stubs = [m for m in ctx.history if m["role"] == "tool" and m["content"].startswith("[evicted")]
    assert stubs and "artifact_recall(" in stubs[0]["content"]
    fake.on("summarize", lambda req: Scripted(content="- decisions: none\n- steps 0-6 done"))
    assert await compaction.fold(app, conv) is True
    conv = app.conversation(desk)
    assert conv["summary"].startswith("- decisions") and conv["summary_cutoff"] > 0
    ctx2 = context.assemble(app, conv, [], None)
    assert len(ctx2.history) < len(ctx.history)
    assert "Summary of earlier conversation" in ctx2.messages(app)[1]["content"]
    kinds = [e["kind"] for e in _events(app, desk, ["compaction"])]
    assert kinds == ["compaction", "compaction"]


def test_calibration_clamps_and_saturation_only_raises(app):
    desk = app.desk_id()
    conv = app.conversation(desk)
    assert compaction.calibrate(app, conv, prompt_tokens=1000, prompt_chars=4000) == pytest.approx(1.0)
    assert compaction.calibrate(app, conv, prompt_tokens=100, prompt_chars=4000) == 0.5
    conv["token_factor"] = 2.0
    eff, _m, _l = compaction.limits(app, conv)
    assert compaction.calibrate(app, conv, prompt_tokens=int(eff * 0.99), prompt_chars=eff * 40) == 2.0


async def test_occupancy_endpoint_shape(app):
    desk = app.desk_id()
    occ = context.occupancy(app, app.conversation(desk))
    assert set(occ) >= {"occupancy_tokens", "limit_tokens", "effective_ctx", "token_factor"}
    assert occ["limit_tokens"] < occ["effective_ctx"]


# -- REST ----------------------------------------------------------------------------------

async def test_messages_endpoint_runs_turn(client, fake):
    app = client.loom
    desk = app.desk_id()
    fake.script(Scripted(content="pong"))
    r = await client.post(f"/api/conversations/{desk}/messages", json={"content": "ping"})
    assert r.status_code == 202
    for _ in range(50):
        await asyncio.sleep(0.02)
        if [e["kind"] for e in _events(app, desk)] == ["user", "assistant"]:
            break
    r = await client.get(f"/api/conversations/{desk}/events")
    assert [e["kind"] for e in r.json()] == ["user", "assistant"]
    r = await client.get("/api/conversations")
    assert r.json()[0]["kind"] == "desk"
    app.vault.write("projects/x.md", "Project x.", op="create", frontmatter={"type": "project"})
    r = await client.post("/api/conversations", json={"goal": "build x", "notes": ["projects/x.md"]})
    assert r.status_code == 201 and r.json()["kind"] == "thread" and r.json()["pinned_notes"] == ["projects/x.md"]
    tid = r.json()["id"]
    assert (await client.post("/api/conversations", json={"goal": "build y", "notes": ["projects/y.md"]})).status_code == 400
    # the person edits the pinned list through PATCH; a missing note is refused whole
    r = await client.patch(f"/api/conversations/{tid}", json={"pinned_notes": []})
    assert r.status_code == 200 and r.json()["pinned_notes"] == []
    r = await client.patch(f"/api/conversations/{tid}", json={"pinned_notes": ["projects/x.md", "projects/y.md"]})
    assert r.status_code == 400 and "projects/y.md" in r.json()["detail"]
    assert (await client.get(f"/api/conversations/{tid}")).json()["pinned_notes"] == []
    fake.script(Scripted(content="starting"))
    r = await client.get(f"/api/conversations/{desk}/occupancy")
    assert r.status_code == 200 and "limit_tokens" in r.json()


# -- titles and queued input --------------------------------------------------------------

def test_default_thread_title_is_a_label_not_a_truncated_paragraph():
    from loom.conversation.tools import default_title

    assert default_title("Add play-random to the music bot.") == "Add play-random to the music bot"
    assert default_title("Check the printer. Then report back.") == "Check the printer"
    assert default_title("  \nsecond line is first\n") == "second line is first"
    goal = (
        "Scan the local 10.0.0.0/24 network to find three 3D printers: an Elegoo Centauri Carbon (DHCP), "
        "an Elegoo Neptune 4 Max (DHCP), and an Ender 3 running Klipper/Moonraker (static IP)."
    )
    title = default_title(goal)
    assert title.endswith("…") and len(title) <= 81
    assert goal.startswith(title[:-1] + " ")  # cut on a word boundary, never mid-word


async def test_message_posted_during_the_last_model_call_is_queued_then_answered(app, fake):
    """`inbox.posted` carries the content, the row is listable while the turn runs, and
    a clean turn re-wakes for rows it had no step boundary left to claim."""
    desk = app.desk_id()
    seen = _collect(app)
    fake.script(Scripted(content="first answer", delay_s=0.3), Scripted(content="second answer"))
    conversation.post(app, desk, "first")
    await asyncio.sleep(0.05)  # the turn has claimed "first" and is inside the model call
    second = conversation.post(app, desk, "second message")

    posted = [e for e in seen if e["type"] == "inbox.posted"][-1]
    assert posted["conversation_id"] == desk
    assert posted["data"]["id"] == second and posted["data"]["source"] == "user"
    assert posted["data"]["content"] == "second message" and posted["data"]["created_at"] and posted["data"]["meta"] == {}
    pending = inbox.pending_rows(app, desk)
    assert [r["id"] for r in pending] == [second]
    assert set(pending[0]) == {"id", "conversation_id", "source", "content", "meta", "created_at"}

    await _run(app, desk)
    await asyncio.sleep(0.01)  # the re-wake is scheduled with call_soon
    await _settle(app)
    assert inbox.pending_rows(app, desk) == []
    users = [e["content"] for e in _events(app, desk, ["user"])]
    answers = [e["content"] for e in _events(app, desk, ["assistant"])]
    assert users[-2:] == ["first", "second message"] and answers[-2:] == ["first answer", "second answer"]
    assert app.conversation(desk)["status"] == "idle"


async def test_reasoning_replay_keeps_the_tail_of_long_reasoning(app, fake, tmp_path):
    """In-turn replay carries the end of a step's reasoning (what led to its calls),
    prefixed by the elision marker, never the start where a loop's seed would sit."""
    from loom import prompts

    desk = app.desk_id()
    app.settings.set(app.db, "conversation.reasoning_replay_max_chars", 60)
    target = tmp_path / "loom-home" / "artifacts" / "note.txt"
    target.write_text("hello", encoding="utf-8")
    long_reasoning = "PLAN " * 40 + "END-OF-THOUGHT reads the file"
    fake.script(
        Scripted(content="", reasoning=long_reasoning, tool_calls=[("fs_read", {"path": str(target)})]),
        Scripted(content="done"),
    )
    conversation.post(app, desk, "read the note")
    await _run(app, desk)
    second = [c for c in fake.calls if c.purpose == "turn"][1]
    assistant = [m for m in second.messages if m.get("role") == "assistant" and m.get("tool_calls")][-1]
    replayed = assistant["reasoning"]
    marker = prompts.load("context/reasoning-elided")
    assert replayed.startswith(marker)
    assert replayed.endswith("END-OF-THOUGHT reads the file")
    assert len(replayed) <= len(marker) + 1 + 60
    assert replayed.count("PLAN") < 20  # the 40-token head is gone; only the tail's few remain


async def test_context_block_lists_the_roots_and_protected_paths(app, fake, tmp_path, monkeypatch):
    """Slot [6]: the model sees where it may work instead of learning it by failing, and
    what Loom itself is (address, pid, home) and which shell it commands — it once
    mistook a music bot's port for its own and lost an hour to Git Bash rewriting `/F`
    as a path (2026-09-22)."""
    import os

    from loom.tools import fs_guard
    from loom.tools.native import shell

    desk = app.desk_id()
    extra = tmp_path / "extra-root"
    extra.mkdir()
    fs_guard.add_root(app, str(extra), added_by="test")
    fs_guard.add_protected(app, str(extra / "secrets.env"))
    app.bind_host, app.bind_port = "0.0.0.0", 8599  # what serve() records; a LAN bind shows the loopback address
    monkeypatch.setattr(shell, "resolve_flavour", lambda app: "gitbash")
    fake.script(Scripted(content="ok"))
    conversation.post(app, desk, "where can you write?")
    await _run(app, desk)
    block = _ctx_block([c for c in fake.calls if c.purpose == "turn"][-1])
    assert "# Filesystem" in block
    assert "roots (relative paths resolve against the first): " in block
    assert fs_guard.normalise(str(app.vault_dir)) in block and fs_guard.normalise(str(extra)) in block
    assert "protected (always ask): " in block and "secrets.env" in block
    assert "projects root" not in block  # the setting is gone (2026-09-22)
    assert f"loom: this backend serves http://127.0.0.1:8599 (pid {os.getpid()}); its home is {str(app.home).replace(chr(92), '/')}; never stop or bind over it" in block
    assert "shell: gitbash — Windows switches must be written //F, //FI, //PID (a single slash is rewritten as a path); redirect to /dev/null, never nul; 2>nul creates a file" in block

    # Without a recorded bind the settings speak, and a flavour without quirks gets no hint.
    app.bind_host, app.bind_port = "", 0
    monkeypatch.setattr(shell, "resolve_flavour", lambda app: "sh")
    line = context.loom_line(app)
    assert line.startswith(f"loom: this backend serves http://127.0.0.1:{int(app.settings.get('general.port'))} (pid ")
    assert context.shell_line(app) == "shell: sh"


async def test_pinned_notes_render_in_slot_five_with_a_cap_and_a_missing_marker(app, fake):
    """Any conversation's pinned notes ride in slot [5], each cut at
    `conversation.pinned_note_chars` with a visible marker; a note deleted after pinning
    says so instead of vanishing."""
    app.settings.set(app.db, "conversation.pinned_note_chars", 500)
    body = "The Ender 3. " * 100  # 1300 chars
    app.vault.write("devices/ender-3.md", body, op="create", frontmatter={"type": "device"})
    app.vault.write("devices/gone.md", "Soon deleted.", op="create", frontmatter={"type": "device"})
    row = conversation.create_thread(app, goal="heat the printer", notes=["devices/ender-3.md", "devices/gone.md"], created_by="user")
    app.vault.delete("devices/gone.md")
    fake.default = lambda req: Scripted(content="ok")
    fake.script(Scripted(content="ok"))
    await _run(app, row["id"])
    await _settle(app)
    block = _ctx_block([c for c in fake.calls if c.conversation_id == row["id"] and c.purpose == "turn"][0])
    stripped = body.strip()
    assert "# Pinned notes\n\n## devices/ender-3.md\n" + stripped[:500] in block
    assert f"[… {len(stripped) - 500} more chars; knowledge_read for the rest]" in block
    assert "## devices/gone.md\n(missing)" in block
    # the desk pins too: its slot [5] is the now board, then its pinned notes
    desk = app.desk_id()
    pins.pin(app, app.conversation(desk), "devices/ender-3.md")
    fake.script(Scripted(content="ok"))
    conversation.post(app, desk, "hi")
    await _run(app, desk)
    desk_block = _ctx_block([c for c in fake.calls if c.conversation_id == desk and c.purpose == "turn"][-1])
    assert "# Now board" in desk_block and "# Pinned notes\n\n## devices/ender-3.md" in desk_block
    assert desk_block.index("# Now board") < desk_block.index("# Pinned notes") < desk_block.index("# Filesystem")
    # pinned notes are already in context, so knowledge resolution skips them
    paths, _text = await context.resolve_knowledge(app, app.conversation(desk), "heat the ender 3")
    assert "devices/ender-3.md" not in paths
