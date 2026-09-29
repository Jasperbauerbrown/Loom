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
