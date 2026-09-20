"""The four walkthroughs of docs/01-walkthroughs.md as end-to-end tests through the
REST API with the fake model (03 §18). Walkthrough 4 (homework via MCP) lives in
tests/test_walkthrough_homework.py because it needs the fake MCP server."""

from __future__ import annotations

import asyncio

from loom.conversation import events
from loom.models.fake import Scripted


async def _settle(app, timeout=5.0):
    """Wait until no turn task is pending (wakes cascade desk → thread → desk)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        pending = [t for n, t in app.tasks.items() if n.startswith("turn:") and not t.done()]
        if not pending:
            await asyncio.sleep(0.02)
            pending = [t for n, t in app.tasks.items() if n.startswith("turn:") and not t.done()]
            if not pending:
                return
        await asyncio.gather(*pending, return_exceptions=True)
    raise AssertionError("turns did not settle")


def _kinds(app, cid):
    return [e["kind"] for e in events.list_events(app, cid, limit=10_000)]


# ---------------------------------------------------------------------------------
# 1. "Heat up the Ender 3."  resolution + classify allow + secret substitution
# ---------------------------------------------------------------------------------
async def test_walkthrough_printer(client, fake, monkeypatch):
    app = client.loom
    desk = app.desk_id()
    # Earlier, once: the familiarisation wrote the device and its procedure.
    app.vault.write(
        "devices/ender-3.md",
        "Ender 3 on the LAN at 192.168.1.50 running Moonraker. Control: [[ender-3-control]].",
        op="create",
        frontmatter={"title": "Ender 3", "aliases": ["ender 3", "the printer", "3d printer"], "type": "device"},
    )
    app.vault.write(
        "procedures/ender-3-control/SKILL.md",
        "Heat: POST http://192.168.1.50/printer/gcode/script with X-Api-Key {{secret:ender_api_key}} and script M104 S200.",
        op="create",
        frontmatter={"name": "ender-3-control", "description": "Heat, cool and query the Ender 3"},
    )
    app.secrets.set("ender_api_key", "sekrit-key-123")
    app.index.rebuild()

    # The http tool is `classify`; the judge sees the request and allows.
    classify_inputs = []

    def judge(req):
        classify_inputs.append(req.messages[-1]["content"])
        return Scripted(content='{"decision":"allow","reason":"user asked to heat the printer"}')

    fake.on("classify", judge)

    # Model: act inline with one http POST carrying the secret reference; then reply.
    seen_http = {}

    async def fake_post(ctx, args):
        seen_http.update(args)
        from loom.tools.registry import ToolResult

        return ToolResult("HTTP 200 application/json\n\n{\"result\": \"ok\"}")

    tool = app.tools.get("http")
    monkeypatch.setattr(tool, "handler", fake_post)
    fake.script(
        Scripted(
            content="",
            tool_calls=[("http", {"method": "POST", "url": "http://192.168.1.50/printer/gcode/script",
                                  "headers": {"X-Api-Key": "{{secret:ender_api_key}}"}, "body": {"script": "M104 S200"}})],
        ),
        Scripted(content="Heating to 200/60."),
    )
    r = await client.post(f"/api/conversations/{desk}/messages", json={"content": "Heat up the Ender 3."})
    assert r.status_code == 202
    await _settle(app)

    # resolution pre-loaded the entity AND its procedure (slot [9])
    turn_req = [c for c in fake.calls if c.purpose == "turn"][0]
    knowledge_msg = [m for m in turn_req.messages if m["role"] == "user" and "<knowledge" in m["content"]][0]
    assert "### devices/ender-3.md" in knowledge_msg["content"]
    assert "### procedures/ender-3-control/SKILL.md" in knowledge_msg["content"]
    assert "{{secret:ender_api_key}}" in knowledge_msg["content"]  # the reference, never the value
    # classifier saw the user's request and the call
    assert "Heat up the Ender 3" in classify_inputs[0] and "gcode" in classify_inputs[0]
    # secret substituted only at execution; transcript keeps the reference
    assert seen_http["headers"]["X-Api-Key"] == "sekrit-key-123"
    tc = events.list_events(app, desk, kinds=["tool_call"])[0]
    assert "sekrit-key-123" not in tc["content"] and "{{secret:ender_api_key}}" in tc["content"]
    tr = events.list_events(app, desk, kinds=["tool_result"])[0]
    assert tr["meta"]["policy"] == "classify" and tr["meta"]["decision"] == "allow"
    assert _kinds(app, desk) == ["user", "tool_call", "tool_result", "assistant"]
    assert events.list_events(app, desk, kinds=["assistant"])[0]["content"] == "Heating to 200/60."
    # cost: resolve + 1 model call + 1 tool + 1 model call (+ 1 classifier)
    assert len([c for c in fake.calls if c.purpose == "turn"]) == 2
    assert len([c for c in fake.calls if c.purpose == "classify"]) == 1


# ---------------------------------------------------------------------------------
# 2. "Add a play-random feature to the music bot."  spawn → status → finish → parent
# ---------------------------------------------------------------------------------
async def test_walkthrough_music_bot(client, fake, tmp_path):
    app = client.loom
    desk = app.desk_id()
    project_root = tmp_path / "music-bot"
    project_root.mkdir()
    (project_root / "bot.js").write_text("// bot\n", encoding="utf-8")
    app.vault.write(
        "projects/music-bot.md",
        "Node Discord music bot. Run: npm start. Test: npm test.",
        op="create",
        frontmatter={"title": "Music bot", "aliases": ["music bot", "the bot"], "type": "project", "root": str(project_root)},
    )
    app.index.rebuild()
    fake.on("classify", lambda req: Scripted(content='{"decision":"allow","reason":"in-scope test run"}'))

    fake.script(
        # desk: resolve → spawn (judgment) → reply
        Scripted(content="", tool_calls=[("conversation_create", {
            "goal": "Add a play-random command to the music bot; keep the existing queue semantics; run npm test.",
            "scope_note": "projects/music-bot.md",
            "excerpt": "User: Add a play-random feature to the music bot.",
            "roots": [str(project_root)],
        })]),
        Scripted(content="Started a thread for the music bot; I'll say when it's ready."),
        # thread: status (plan) → read → edit → test → status → finish
        Scripted(content="", tool_calls=[("status_update", {"state": "planning", "goal": "play-random command", "next": "read bot.js, implement, test"})]),
        Scripted(content="", tool_calls=[("fs_read", {"path": str(project_root / "bot.js")})]),
        Scripted(content="", tool_calls=[("fs_edit", {"path": str(project_root / "bot.js"), "old": "// bot\n", "new": "// bot\nfunction playRandom() {}\n"})]),
        Scripted(content="", tool_calls=[("shell", {"command": "echo 1 passing", "cwd": str(project_root)})]),
        Scripted(content="", tool_calls=[("status_update", {"state": "working", "done": "- implemented playRandom\n- tests pass", "doing": "finishing"})]),
        Scripted(content="", tool_calls=[("conversation_finish", {
            "summary": "play-random implemented in bot.js; npm test passes.",
            "walkthrough": "Changed bot.js (added playRandom). Verified: npm test → 1 passing on 2026-09-20.",
        })]),
        # desk: verifies by judgment on the thread event and tells the user
        Scripted(content="The music bot thread finished: play-random added and tests pass."),
    )
    r = await client.post(f"/api/conversations/{desk}/messages", json={"content": "Add a play-random feature to the music bot."})
    assert r.status_code == 202
    await _settle(app)

    threads = (await client.get("/api/conversations?kind=thread")).json()
    assert len(threads) == 1
    t = threads[0]
    assert t["status"] == "done" and t["scope_note"] == "projects/music-bot.md" and t["parent_id"] == desk
    assert (project_root / "bot.js").read_text(encoding="utf-8").startswith("// bot\nfunction playRandom")
    # thread transcript: the shell call was classified (allowed) and its result recorded
    kinds = _kinds(app, t["id"])
    assert kinds.count("tool_call") == 6 and kinds.count("tool_result") == 6
    # status block and walkthrough in the vault
    fields, owner = app.vault.read_status("projects/music-bot.md")
    assert fields["state"] == "done" and owner == t["id"]
    walkthroughs = [p for p in app.vault.list("projects/music-bot/") if "walkthrough-" in p]
    assert len(walkthroughs) == 1
    assert "[[projects/music-bot/walkthrough-" in app.vault.read("projects/music-bot.md").body
    # the desk heard about it and answered; the now board carries the item
    desk_kinds = _kinds(app, desk)
    assert desk_kinds[-2:] == ["user", "assistant"]
    items_rows = (await client.get("/api/items")).json()
    assert any(i["kind"] == "thread_event" and "finished" in i["title"] for i in items_rows)
    # thread context: scope note with status block in slot [5] and the budget line
    thread_call = [c for c in fake.calls if c.conversation_id == t["id"] and c.purpose == "turn"][0]
    assert "projects/music-bot.md" in thread_call.messages[1]["content"]
    assert "[steps remaining:" in thread_call.messages[1]["content"]
    # thread steps ran at background priority, desk turns at interactive priority
    prios = {c.conversation_id: p for c, p in zip(fake.calls, fake.priorities) if c.purpose == "turn"}
    assert prios[desk] == 0 and prios[t["id"]] == 1


# ---------------------------------------------------------------------------------
# 3. "I need a program to visualise coordinates on a 3D map."  status answered from the note
# ---------------------------------------------------------------------------------
async def test_walkthrough_3d_map(client, fake):
    app = client.loom
    desk = app.desk_id()
    fake.script(
        # desk: nothing resolves; it creates the project note BEFORE spawning (state written
        # before acting on it), with the aliases people will use for it
        Scripted(content="", tool_calls=[("knowledge_write", {
            "path": "projects/3d-map-viz.md", "op": "create",
            "content": "Desktop program visualising coordinates and datapoints on a 3D map. Input: CSV. Created 2026-09-20.",
            "frontmatter": {"title": "3D map viz", "aliases": ["3d map", "3d map project", "map data project"], "type": "project"},
        })]),
        Scripted(content="", tool_calls=[("conversation_create", {
            "goal": "Build a desktop program that visualises coordinates and datapoints on a 3D map.",
            "scope_note": "projects/3d-map-viz.md",
            "excerpt": "User: I need a program to visualise coordinates and datapoints on a 3D map. Assume desktop, CSV input.",
        })]),
        Scripted(content="Started; assuming desktop + CSV input. Tell me if that's wrong."),
        # thread: first status write is the plan, then it "keeps working" and stops to ask
        Scripted(content="", tool_calls=[("status_update", {"state": "working", "goal": "3D map viz desktop app", "doing": "scaffolding the project", "next": "- choose renderer\n- CSV loader"})]),
        Scripted(content="Should the renderer be web-based (three.js in a webview) or native OpenGL?"),
        # desk: the thread's question arrives as an event; the desk relays it to the person
        Scripted(content="The 3D map thread asks: web-based renderer or native OpenGL?"),
    )
    r = await client.post(f"/api/conversations/{desk}/messages", json={"content": "I need a program to visualise coordinates and datapoints on a 3D map."})
    assert r.status_code == 202
    await _settle(app)
    t = (await client.get("/api/conversations?kind=thread")).json()[0]
    assert t["status"] == "waiting_input"
    assert t["scope_note"] and app.vault.exists(t["scope_note"])
    assert t["status_line"] == "scaffolding the project"

    # Meanwhile the user asks the desk for the state: answered from the note, no thread read.
    fake.script(Scripted(content="The 3D map project is scaffolding; next: choose renderer, CSV loader. It is waiting on a renderer decision."))
    r = await client.post(f"/api/conversations/{desk}/messages", json={"content": "What's the state of the 3D map project?"})
    await _settle(app)
    last = [c for c in fake.calls if c.purpose == "turn"][-1]
    knowledge_msg = [m for m in last.messages if m["role"] == "user" and "<knowledge" in m["content"]]
    assert knowledge_msg, "the project note was resolved by alias and pre-loaded"
    assert "scaffolding the project" in knowledge_msg[0]["content"]
    assert "status:begin" in knowledge_msg[0]["content"]
    assert "3D map" in last.messages[1]["content"]  # now board lists the waiting thread
    assert not any(c.conversation_id == t["id"] for c in fake.calls[-1:])  # no thread turn was run

    # The person answers on the desk; the desk forwards into the thread's inbox; the thread resumes.
    fake.script(
        Scripted(content="", tool_calls=[("conversation_send", {"conversation_id": t["id"], "content": "Use three.js in a webview."})]),
        Scripted(content="Told the thread: three.js in a webview."),
        Scripted(content="", tool_calls=[("status_update", {"state": "working", "doing": "wiring three.js renderer", "blockers": ""})]),
        Scripted(content="Renderer decided; continuing."),
        Scripted(content="The 3D map thread is continuing with three.js."),
    )
    r = await client.post(f"/api/conversations/{desk}/messages", json={"content": "Web-based, three.js."})
    await _settle(app)
    thread_users = events.list_events(app, t["id"], kinds=["user"])
    assert any("three.js in a webview" in e["content"] and '[from desk' in e["content"] for e in thread_users)
    fields, _ = app.vault.read_status(t["scope_note"])
    assert fields["doing"] == "wiring three.js renderer"
