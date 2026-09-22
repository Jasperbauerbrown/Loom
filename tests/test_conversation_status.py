"""Thread status (03 §6.7, decided 2026-09-22): one reader, one writer, one sink. Every
thread's status lives in its own transcript as `status` events and its walkthrough lands
in the daily log; the notes a thread pins are never written by the harness."""

from __future__ import annotations

import asyncio

from loom import conversation
from loom.conversation import context, events, runner
from loom.conversation.tools import local_date
from loom.models.fake import Scripted


async def _run(app, cid):
    await runner.run_turn(app, cid)
    task = app.tasks.get(f"turn:{cid}")
    if task is not None and not task.done():
        await task


async def _settle(app):
    for _ in range(20):
        pending = [t for n, t in app.tasks.items() if n.startswith("turn:") and not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)


def _quiet_desk(fake):
    """The parent wakes on every thread event; answer it with plain text."""
    fake.default = lambda req: Scripted(content="ok")


async def test_status_update_on_a_task_writes_transcript_events(app, fake):
    _quiet_desk(fake)
    row = conversation.create_thread(app, goal="Check whether the NAS answers on 443.", created_by="user")
    assert row["pinned_notes"] == []
    fake.script(
        Scripted(content="", tool_calls=[("status_update", {"state": "working", "doing": "probing port 443"})]),
        Scripted(content="", tool_calls=[("status_update", {"next": "report to the desk"})]),
        Scripted(content="Port 443 is open."),
    )
    await _run(app, row["id"])
    status_events = events.list_events(app, row["id"], kinds=["status"])
    assert len(status_events) == 2
    latest = status_events[-1]
    # fields accumulate across events like the note block does; the content is one line
    assert latest["meta"]["fields"]["state"] == "working"
    assert latest["meta"]["fields"]["doing"] == "probing port 443"
    assert latest["meta"]["fields"]["next"] == "report to the desk"
    assert latest["meta"]["owner"] == row["id"] and latest["meta"]["fields"]["updated"]
    assert latest["content"].startswith("state: working")
    fields, owner, source = conversation.status_fields(app, app.conversation(row["id"]))
    assert source == "transcript" and owner == row["id"] and fields["doing"] == "probing port 443"
    # the tool result names the sink; the desk's now board reads the same fields
    results = [e["content"] for e in events.list_events(app, row["id"], kinds=["tool_result"])]
    assert results[0].startswith("status recorded:")
    assert "probing port 443" in context.now_board(app)
    await _settle(app)


async def test_safety_net_writes_a_status_event_for_a_task(app, fake):
    _quiet_desk(fake)
    row = conversation.create_thread(app, goal="write docs", created_by="user")
    fake.on("status", lambda req: Scripted(content='{"state":"waiting","goal":"write docs","done":"","doing":"","next":"answer question","blockers":""}'))
    fake.script(Scripted(content="Which format do you want?"))
    await _run(app, row["id"])
    fields, owner, source = conversation.status_fields(app, app.conversation(row["id"]))
    assert source == "transcript" and owner == row["id"]
    assert fields["state"] == "waiting" and fields["next"] == "answer question"
    assert app.conversation(row["id"])["status"] == "waiting_input"
    await _settle(app)


async def test_finish_on_a_task_appends_to_the_daily_log(app, fake):
    _quiet_desk(fake)
    first = conversation.create_thread(app, goal="First task: check the printer.", created_by="user")
    fake.script(Scripted(content="", tool_calls=[("conversation_finish", {"summary": "Done A.", "walkthrough": "Checked A; evidence in the transcript."})]))
    await _run(app, first["id"])
    await _settle(app)

    path = f"log/{local_date(app)}.md"
    assert app.vault.exists(path)
    note = app.vault.read(path)
    assert note.type == "log" and note.title == local_date(app)
    assert f"## {first['title']} ({first['id']})" in note.body and "state: done" in note.body and "Checked A" in note.body
    result = events.last(app, first["id"], 1, kinds=["tool_result"])[0]
    assert path in result["content"]
    fields, _owner, source = conversation.status_fields(app, app.conversation(first["id"]))
    assert source == "transcript" and fields["state"] == "done" and fields["done"] == "Done A."
    assert app.conversation(first["id"])["status"] == "done"
    assert app.conversation(first["id"])["walkthrough"] == path  # the row remembers where it filed
    assert not app.vault.list("projects/")  # no dead project note left behind

    # a second task the same day appends a section rather than failing on `create`
    second = conversation.create_thread(app, goal="Second task: check the NAS.", created_by="user")
    fake.script(Scripted(content="", tool_calls=[("conversation_finish", {"summary": "Done B.", "walkthrough": "Checked B."})]))
    await _run(app, second["id"])
    await _settle(app)
    body = app.vault.read(path).body
    assert "Checked A" in body and "Checked B" in body
    assert body.count("\n## ") == 2


async def test_status_never_touches_a_pinned_note(app, fake):
    """A thread with a pinned project note still keeps its status in its transcript; the
    note is the model's knowledge and the harness never writes into it (2026-09-22)."""
    _quiet_desk(fake)
    app.vault.write("projects/music-bot.md", "Node Discord music bot.", op="create", frontmatter={"type": "project"})
    row = conversation.create_thread(app, goal="Add play-random to the music bot.", notes=["projects/music-bot.md"], created_by="user")
    fake.script(
        Scripted(content="", tool_calls=[("status_update", {"state": "working", "doing": "reading bot.js"})]),
        Scripted(content="Reading."),
    )
    await _run(app, row["id"])
    fields, owner, source = conversation.status_fields(app, app.conversation(row["id"]))
    assert source == "transcript" and owner == row["id"] and fields["doing"] == "reading bot.js"
    assert len(events.list_events(app, row["id"], kinds=["status"])) == 1
    results = [e["content"] for e in events.list_events(app, row["id"], kinds=["tool_result"])]
    assert results[0].startswith("status recorded:")
    assert app.vault.read("projects/music-bot.md").body.strip() == "Node Discord music bot."
    await _settle(app)


async def test_status_endpoints(client, fake):
    app = client.loom
    _quiet_desk(fake)
    fake.on("status", lambda req: Scripted(content='{"state":"working","doing":"starting"}'))
    task = conversation.create_thread(app, goal="Check the printer.", created_by="user")
    await _settle(app)  # the turn ends in text; the safety net writes a first status

    t = (await client.get(f"/api/conversations/{task['id']}/status")).json()
    assert t["source"] == "transcript" and t["owner"] == task["id"] and t["fields"]["doing"] == "starting"

    # the person edits through the same writer; the event records who wrote it
    t2 = (await client.put(f"/api/conversations/{task['id']}/status", json={"fields": {"next": "report"}})).json()
    assert t2["source"] == "transcript" and t2["fields"]["next"] == "report" and t2["fields"]["doing"] == "starting"
    assert t2["owner"] == "user"

    # the row's status line follows
    assert (await client.get(f"/api/conversations/{task['id']}")).json()["status_line"] == "starting"

    desk = app.desk_id()
    assert (await client.get(f"/api/conversations/{desk}/status")).json() == {"fields": None, "owner": None, "source": "none"}
    assert (await client.put(f"/api/conversations/{desk}/status", json={"fields": {"state": "working"}})).status_code == 400
    assert (await client.put(f"/api/conversations/{task['id']}/status", json={"fields": {}})).status_code == 400
    assert (await client.get("/api/conversations/nope/status")).status_code == 404
