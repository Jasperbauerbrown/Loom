"""Walkthrough 4: "What homework do I have this week?" — MCP tools (read-only → allow
via annotations), syllabus facts from pre-loaded course notes, inline knowledge_write,
and the follow-up that becomes an explicit schedule (docs/01-walkthroughs.md §4)."""

from __future__ import annotations

import sys
from pathlib import Path

from loom.conversation import events
from loom.models.fake import Scripted

from tests.test_walkthroughs import _context, _kinds, _settle

SERVER = str(Path(__file__).parent / "fake_mcp_server.py")


async def test_walkthrough_homework(client, fake):
    app = client.loom
    desk = app.desk_id()

    # Earlier, once: Canvas configured as an MCP server (tier 2); familiarisation wrote the notes.
    row = await app.mcp.create(name="canvas", transport="stdio", command=sys.executable, args=[SERVER, "normal"], enabled=True)
    await app.tasks[f"mcp:{row['id']}"]
    assert app.mcp.row(row["id"])["status"] == "connected"
    assert "mcp_canvas_fake_read" in app.tools and "mcp_canvas_fake_echo" in app.tools
    assert app.vault.exists("connectors/canvas.md")  # the integration is also a knowledge entity
    app.vault.write(
        "topics/school.md",
        "Fall 2026 term. Courses: [[cs101]], [[math201]]. Canvas via [[canvas]].",
        op="create",
        frontmatter={"title": "School", "aliases": ["homework", "school", "classes"], "type": "topic"},
    )
    app.vault.write(
        "courses/cs101.md",
        "CS101. Syllabus: weekly problem set due every Friday 23:59 (from the syllabus PDF, 2026-08-25).",
        op="create",
        frontmatter={"title": "CS101", "aliases": ["cs101", "intro cs"], "type": "course"},
    )
    app.vault.write(
        "courses/math201.md",
        "MATH201. Syllabus: quiz every Monday in class.",
        op="create",
        frontmatter={"title": "MATH201", "aliases": ["math201"], "type": "course"},
    )
    app.index.rebuild()

    fake.script(
        # act inline: read-only MCP tool → allowed by annotation, no classifier call
        Scripted(content="", tool_calls=[("mcp_canvas_fake_read", {"path": "courses/upcoming"})]),
        # an assignment found only in mail → written into the course note inline
        Scripted(content="", tool_calls=[("knowledge_write", {
            "path": "courses/cs101.md", "op": "append",
            "content": "Assignment: essay on sorting, due 2026-09-26 (announced by email 2026-09-19).",
        })]),
        Scripted(content="| Course | Item | Due |\n|---|---|---|\n| CS101 | problem set | 2026-09-25 |\n| CS101 | essay on sorting | 2026-09-26 |\n| MATH201 | quiz | 2026-09-22 |"),
    )
    r = await client.post(f"/api/conversations/{desk}/messages", json={"content": "What homework do I have this week?"})
    assert r.status_code == 202
    await _settle(app)

    # resolution: "homework" → topics/school.md + first-hop course notes pre-loaded
    first = [c for c in fake.calls if c.purpose == "turn"][0]
    kb = [m for m in first.messages if m["role"] == "user" and "<knowledge" in m["content"]][0]["content"]
    assert "### topics/school.md" in kb and "### courses/cs101.md" in kb and "### courses/math201.md" in kb
    assert "now:" in _context(first)  # today's date is always in context
    # MCP read-only tool ran without a classifier; the result came back from the server
    assert not any(c.purpose == "classify" for c in fake.calls)
    results = events.list_events(app, desk, kinds=["tool_result"])
    assert results[0]["meta"]["name"] == "mcp_canvas_fake_read" and "contents of courses/upcoming" in results[0]["content"]
    assert results[0]["meta"]["policy"] == "allow"
    # inline consolidation landed in the vault and is committed
    assert "essay on sorting" in app.vault.read("courses/cs101.md").body
    assert any("append courses/cs101.md" in c["message"] for c in app.vault.git.log("courses/cs101.md"))
    assert _kinds(app, desk) == ["user", "tool_call", "tool_result", "tool_call", "tool_result", "assistant"]

    # Follow-up: "remind me the day before each is due" → the model schedules explicitly.
    fake.script(
        Scripted(content="", tool_calls=[
            ("schedule_create", {"spec": "at 2026-09-24T18:00", "payload": "Reminder: CS101 problem set due 2026-09-25 23:59.", "policy": "interrupt"}),
            ("schedule_create", {"spec": "at 2026-09-25T18:00", "payload": "Reminder: CS101 essay on sorting due 2026-09-26.", "policy": "interrupt"}),
        ]),
        Scripted(content="Set two reminders for the day before each due date."),
    )
    r = await client.post(f"/api/conversations/{desk}/messages", json={"content": "Remind me the day before each is due."})
    await _settle(app)
    rows = (await client.get("/api/schedules")).json()
    assert len(rows) == 2 and all(s["policy"] == "interrupt" and s["enabled"] for s in rows)
    assert all(s["created_by"] == f"conversation:{desk}" and s["target"] in ("desk", desk) for s in rows)

    # A destructive MCP tool (no readOnlyHint) falls to `ask`: fail closed without a policy row.
    fake.script(Scripted(content="", tool_calls=[("mcp_canvas_fake_echo", {"text": "post grade"})]))
    r = await client.post(f"/api/conversations/{desk}/messages", json={"content": "Post that to Canvas."})
    await _settle(app)
    assert app.conversation(desk)["status"] == "waiting_approval"
    pending = (await client.get("/api/approvals?status=pending")).json()
    assert len(pending) == 1 and pending[0]["tool"] == "mcp_canvas_fake_echo"
