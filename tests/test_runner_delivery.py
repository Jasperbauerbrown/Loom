"""What a finished thread hands back (TD audit §3).

`conversation_finish` builds two strings: `summary`, the one-line now-board entry, and
`delivery`, the summary with the finish-evidence block under it. The runner posts
`delivery` to the parent conversation - the desk re-derived 43 minutes of work because
the walkthrough was filed and never fed - and keeps `summary` for the board item, which
must stay short.
"""

from __future__ import annotations

import asyncio

from loom import conversation
from loom.conversation import runner
from loom.conversation.tools import local_date
from loom.models.fake import Scripted

SUMMARY = "Ported the game to raylib; window rendering and click-to-build confirmed."
WALKTHROUGH = "Built with cmake; screenshot shows the tower placed. Gotcha: window-matched capture returns occluded content."


async def _run(app, cid):
    await runner.run_turn(app, cid)
    task = app.tasks.get(f"turn:{cid}")
    if task is not None and not task.done():
        await task


def _inbox(app, cid: str, source: str) -> list[str]:
    rows = app.db.execute(
        "SELECT content FROM inbox WHERE conversation_id=? AND source=? ORDER BY id", (cid, source)
    ).fetchall()
    return [r["content"] for r in rows]


async def test_a_finish_delivers_the_evidence_to_the_parent_and_a_summary_to_the_board(app, fake):
    desk = app.desk_id()
    fake.default = lambda req: Scripted(content="ok")
    row = conversation.create_thread(app, goal="port the game to raylib", created_by="user")
    cid = row["id"]
    fake.script(Scripted(content="", tool_calls=[("conversation_finish", {"summary": SUMMARY, "walkthrough": WALKTHROUGH})]))
    await _run(app, cid)
    await asyncio.sleep(0)

    assert app.conversation(cid)["status"] == "done"
    bodies = _inbox(app, desk, f"conversation:{cid}")
    assert len(bodies) == 1
    body = bodies[0]
    # the parent reads the summary first, then the evidence under it
    assert body.startswith(f"[finished]\n{SUMMARY}")
    assert f"# Walkthrough — log/{local_date(app)}.md" in body
    assert WALKTHROUGH in body

    # the now-board item stays the summary alone
    item = app.db.execute("SELECT title, body FROM items WHERE kind='thread_event'").fetchone()
    assert item["body"] == SUMMARY
    assert "# Walkthrough" not in item["body"]


async def test_a_thread_that_stops_without_finishing_delivers_its_text_unchanged(app, fake):
    """`failed` and `waiting` have no evidence block; the body is the text, as before."""
    desk = app.desk_id()
    fake.default = lambda req: Scripted(content="ok")
    fake.on("status", lambda req: Scripted(content='{"state":"waiting","goal":"g","done":"","doing":"","next":"answer","blockers":""}'))
    row = conversation.create_thread(app, goal="write docs", created_by="user")
    fake.script(Scripted(content="Which format do you want?"))
    await _run(app, row["id"])

    bodies = _inbox(app, desk, f"conversation:{row['id']}")
    assert bodies == ["[stopped without finishing]\nWhich format do you want?"]
