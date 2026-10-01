"""The maintenance role (03 §8.6): consolidation, curation, housekeeping, scheduling.

Every pass here runs against the fake model client; the scripted `maintenance` responder
stands in for the model's judgment, and the assertions are about what the pass *did* —
notes on disk, watermarks, approvals, items, archived rows, commits.
"""

from __future__ import annotations

import datetime as _dt
import time

import pytest

from loom import db, items
from loom.conversation import events
from loom.knowledge import maintenance as M
from loom.models.fake import Scripted
from loom.tools import approvals


def responder(*scripts: Scripted):
    """A `fake.on("maintenance", ...)` responder: the scripts in order, then silence."""
    queue = list(scripts)

    def respond(_request):
        return queue.pop(0) if queue else Scripted(content="nothing to record")

    return respond


def write(*calls: tuple[str, dict]) -> Scripted:
    return Scripted(tool_calls=list(calls))


def days_ago(n: int) -> str:
    moment = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=n)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------------
# 1. Consolidation
# ---------------------------------------------------------------------------------
async def test_consolidation_writes_a_note_advances_the_watermark_and_commits(app, fake):
    cid = app.desk_id()
    events.append(app, cid, "user", "The Ender 3 lives at 192.168.1.40 in the workshop.")
    events.append(app, cid, "assistant", "Noted — I'll remember the address.")
    fake.on(
        "maintenance",
        responder(
            write((
                "knowledge_write",
                {
                    "path": "devices/ender-3.md",
                    "op": "create",
                    "content": "A 3D printer at 192.168.1.40, in the workshop.",
                    "frontmatter": {"aliases": ["ender 3"]},
                },
            )),
            Scripted(content="recorded"),
        ),
    )

    report = await app.maintenance.run("test")

    assert report["reason"] == "test" and report["errors"] == []
    assert report["consolidated"] == [{"conversation_id": cid, "events": 2, "writes": 1}]
    assert "192.168.1.40" in app.vault.read("devices/ender-3.md").body
    assert app.conversation(cid)["maint_watermark"] == events.max_id(app, cid)
    assert any("create devices/ender-3.md" in c["message"] for c in app.vault.git.log())
    assert app.maintenance.report is report and report["finished_at"]


async def test_a_second_pass_with_no_new_events_calls_no_model(app, fake):
    cid = app.desk_id()
    events.append(app, cid, "user", "Nothing durable here.")
    fake.on("maintenance", responder(Scripted(content="nothing to record")))
    await app.maintenance.run("test")
    before = len(fake.calls)

    report = await app.maintenance.run("test")

    assert len(fake.calls) == before
    assert report["consolidated"] == []


async def test_events_the_sweep_never_shows_still_advance_the_watermark(app, fake):
    cid = app.desk_id()
    events.append(app, cid, "system", "restarted")
    events.append(app, cid, "tool_result", "ok")

    report = await app.maintenance.run("test")

    assert fake.calls == []
    assert report["consolidated"] == []
    assert app.conversation(cid)["maint_watermark"] == events.max_id(app, cid)


async def test_a_model_failure_is_reported_and_the_watermark_stays_put(app, fake):
    cid = app.desk_id()
    events.append(app, cid, "user", "Something durable.")
    fake.on("maintenance", responder(Scripted(outcome="fatal", error="provider unreachable")))

    report = await app.maintenance.run("test")

    assert any("provider unreachable" in e for e in report["errors"])
    assert app.conversation(cid)["maint_watermark"] == 0


async def test_a_core_write_is_handed_back_to_the_person(app, fake):
    """`knowledge_write:core/` is `ask`, and a pass has no turn to pause: the approval is
    denied at once (no stale row) and the proposal lands on the now board."""
    cid = app.desk_id()
    events.append(app, cid, "user", "Always use metric units.")
    fake.on(
        "maintenance",
        responder(
            write(("knowledge_write", {"path": "core/preferences.md", "op": "append", "content": "- Metric units."})),
            Scripted(content="handed over"),
        ),
    )

    report = await app.maintenance.run("test")

    assert approvals.pending(app) == []
    assert approvals.listed(app, "denied")
    posted = [i for i in items.list_items(app) if i["source"] == "maintenance"]
    assert len(posted) == 1
    assert posted[0]["action"] == {"kind": "knowledge_write", "args": {"path": "core/preferences.md", "op": "append", "content": "- Metric units."}}
    assert "core/preferences.md" in posted[0]["title"]
    assert app.conversation(cid)["status"] != "waiting_approval"
    assert app.conversation(cid)["maint_watermark"] == events.max_id(app, cid)
    assert report["consolidated"][0]["writes"] == 0
    assert "- Metric units." not in app.vault.read("core/preferences.md").body
    assert "- Metric units." in posted[0]["body"]  # the card previews the proposed write


async def test_a_handed_back_replace_previews_old_and_new(app, fake):
    """A `replace` carries old/new, not `content`: the now-board card must still show
    the proposed edit (2026-10-01 — it used to show only the bare reason)."""
    cid = app.desk_id()
    events.append(app, cid, "user", "The bridge note moved.")
    args = {"path": "core/conventions.md", "op": "replace", "old": "see [[old-note]]", "new": "see [[new-note]]"}
    fake.on(
        "maintenance",
        responder(write(("knowledge_write", args)), Scripted(content="handed over")),
    )

    await app.maintenance.run("test")

    posted = [i for i in items.list_items(app) if i["source"] == "maintenance"]
    assert len(posted) == 1
    assert "see [[old-note]]" in posted[0]["body"]
    assert "see [[new-note]]" in posted[0]["body"]


def test_batch_events_splits_oldest_first(app, monkeypatch):
    evs = [
        {"id": i, "kind": "user", "content": "x" * 5000, "meta": {}}
        for i in range(1, 6)
    ]
    batches = M.batch_events(evs)
    assert len(batches) > 1
    assert [e["id"] for e in batches[0]][0] == 1
    assert sum(len(b) for b in batches) == 5
    assert all(len(b) >= 1 for b in batches)


# ---------------------------------------------------------------------------------
# 2. Curation
# ---------------------------------------------------------------------------------
def _two_notes_sharing_an_alias(app):
    app.vault.write(
        "devices/printer.md", "The workshop printer at 192.168.1.40.", op="create",
        frontmatter={"aliases": ["the printer"], "created": "2026-01-01"},
    )
    app.vault.write(
        "devices/ender-3.md", "Ender 3, bed levelled 2026-09-01.", op="create",
        frontmatter={"aliases": ["the printer"], "created": "2026-06-01"},
    )


async def test_alias_collision_is_a_candidate_and_the_merge_is_applied(app, fake):
    _two_notes_sharing_an_alias(app)
    candidates = app.maintenance.candidates()
    assert candidates and candidates[0]["kind"] == "pair"
    assert {candidates[0]["left"], candidates[0]["right"]} == {"devices/printer.md", "devices/ender-3.md"}

    fake.on(
        "maintenance",
        responder(
            write(
                ("knowledge_write", {"path": "devices/printer.md", "op": "append", "content": "Bed levelled 2026-09-01."}),
                ("knowledge_write", {"path": "devices/ender-3.md", "op": "rethink", "content": "Moved to [[printer]].", "reason": "duplicate of devices/printer.md"}),
            ),
            Scripted(content="merged"),
        ),
    )

    report = await app.maintenance.run("test")

    assert report["curated"]["candidates"] >= 1
    assert report["curated"]["applied"] == 2
    assert "Bed levelled 2026-09-01." in app.vault.read("devices/printer.md").body
    assert "Moved to" in app.vault.read("devices/ender-3.md").body


def test_the_older_note_is_presented_first(app):
    _two_notes_sharing_an_alias(app)
    text = app.maintenance._curate_user(
        {"kind": "pair", "left": "devices/ender-3.md", "right": "devices/printer.md", "why": "test"}
    )
    assert "A — devices/printer.md (older)" in text
    assert "B — devices/ender-3.md (newer)" in text


def test_candidates_are_capped(app):
    for i in range(15):
        app.vault.write(f"topics/n{i}.md", f"See [[ghost-{i}]] for details.", op="create")
    candidates = app.maintenance.candidates()
    assert len(candidates) == M.MAX_CANDIDATES
    assert any(c["kind"] == "dangling" for c in candidates)


async def test_a_dangling_link_becomes_a_note(app, fake):
    app.vault.write("projects/map.md", "Deployed on [[nas]] nightly.", op="create")
    fake.on(
        "maintenance",
        responder(
            write(("knowledge_write", {"path": "devices/nas.md", "op": "create", "content": "The NAS the map deploys to.", "frontmatter": {"aliases": ["nas"]}})),
            Scripted(content="created"),
        ),
    )

    report = await app.maintenance.run("test")

    assert report["curated"]["applied"] == 1
    assert app.vault.exists("devices/nas.md")


# ---------------------------------------------------------------------------------
# 3. Housekeeping
# ---------------------------------------------------------------------------------
def _thread(app, cid: str, status: str, updated_at: str, archived_at: str | None = None) -> None:
    app.db.execute(
        "INSERT INTO conversations(id, kind, role, status, created_at, updated_at, archived_at) "
        "VALUES (?, 'thread', 'worker', ?, ?, ?, ?)",
        (cid, status, updated_at, updated_at, archived_at),
    )


async def test_old_finished_threads_are_archived_and_recent_ones_are_not(app, fake):
    _thread(app, "T-old", "done", days_ago(30))
    _thread(app, "T-new", "done", db.now_iso())
    _thread(app, "T-running", "running", days_ago(30))

    report = await app.maintenance.run("test")

    assert report["archived"] == 1
    assert app.conversation("T-old")["status"] == "archived"
    assert app.conversation("T-old")["archived_at"]
    assert app.conversation("T-new")["status"] == "done"
    assert app.conversation("T-running")["status"] == "running"


async def test_artifacts_of_long_archived_conversations_are_pruned(app, fake):
    _thread(app, "T-gone", "archived", days_ago(200), archived_at=days_ago(200))
    _thread(app, "T-fresh", "archived", days_ago(1), archived_at=days_ago(1))
    stale = app.artifacts.spill("T-gone", None, "x" * 50)
    kept = app.artifacts.spill("T-fresh", None, "y" * 50)

    report = await app.maintenance.run("test")

    assert report["pruned_artifacts"] == 1
    assert app.artifacts.get(stale["id"]) is None
    assert app.artifacts.get(kept["id"]) is not None


async def test_external_edits_are_committed(app, fake):
    (app.vault.root / "topics").mkdir(exist_ok=True)
    (app.vault.root / "topics" / "hand-written.md").write_text("Written in Obsidian.\n", encoding="utf-8")

    report = await app.maintenance.run("test")

    assert report["external_commit"]
    message = app.vault.git.log(n=1)[0]["message"]
    assert message.startswith("user: edit") and "hand-written.md" in message


async def test_external_edits_are_left_alone_when_the_setting_is_off(app, fake):
    app.settings.set(app.db, "knowledge.auto_commit_external_edits", False)
    (app.vault.root / "topics").mkdir(exist_ok=True)
    (app.vault.root / "topics" / "hand-written.md").write_text("Written in Obsidian.\n", encoding="utf-8")

    report = await app.maintenance.run("test")

    assert report["external_commit"] is None
    assert "topics/hand-written.md" in app.vault.git.status_porcelain()


def test_external_message_names_up_to_five_paths():
    assert M.external_message(["a.md"]) == "user: edit a.md"
    many = [f"n{i}.md" for i in range(8)]
    message = M.external_message(many)
    assert message.startswith("user: edit 8 files: n0.md")
    assert message.endswith("(+3 more)")


async def test_a_pass_that_changed_something_posts_one_item(app, fake):
    _thread(app, "T-old", "done", days_ago(30))

    await app.maintenance.run("test")

    posted = [i for i in items.list_items(app) if i["source"] == "maintenance"]
    assert len(posted) == 1
    assert posted[0]["title"] == "Maintenance: 1 thread archived"
    assert posted[0]["kind"] == "knowledge"


async def test_a_pass_that_changed_nothing_posts_nothing(app, fake):
    await app.maintenance.run("test")
    assert [i for i in items.list_items(app) if i["source"] == "maintenance"] == []


# ---------------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------------
def test_parse_hhmm():
    assert M.parse_hhmm("03:00") == (3, 0)
    assert M.parse_hhmm("23:45") == (23, 45)
    assert M.parse_hhmm("nonsense") == (3, 0)
    assert M.parse_hhmm("99:99") == (3, 0)


def test_slot_date_and_nightly_due():
    before = _dt.datetime(2026, 9, 20, 2, 0)
    after = _dt.datetime(2026, 9, 20, 3, 1)
    assert M.slot_date(before, "03:00") == "2026-09-19"
    assert M.slot_date(after, "03:00") == "2026-09-20"
    assert not M.nightly_due(before, "03:00", "2026-09-19")
    assert M.nightly_due(after, "03:00", "2026-09-19")
    assert not M.nightly_due(after, "03:00", "2026-09-20")
    assert M.next_slot(before, "03:00") == _dt.datetime(2026, 9, 20, 3, 0)
    assert M.next_slot(after, "03:00") == _dt.datetime(2026, 9, 21, 3, 0)


def test_idle_due():
    assert M.idle_due(idle_for=200, since_last_pass=200, idle_seconds=120, has_work=True)
    assert not M.idle_due(idle_for=None, since_last_pass=999, idle_seconds=120, has_work=True)
    assert not M.idle_due(idle_for=200, since_last_pass=999, idle_seconds=120, has_work=False)
    assert not M.idle_due(idle_for=30, since_last_pass=999, idle_seconds=120, has_work=True)
    assert not M.idle_due(idle_for=200, since_last_pass=10, idle_seconds=120, has_work=True)


async def test_due_reports_nightly_then_idle_and_nothing_when_disabled(app):
    m = app.maintenance
    now = _dt.datetime(2026, 9, 20, 3, 5)
    m._last_slot = "2026-09-19"
    assert await m.due(now) == "nightly"

    m._last_slot = "2026-09-20"
    assert await m.due(now) is None  # idle window not open, no work

    events.append(app, app.desk_id(), "user", "work to consolidate")
    m._idle_since = time.monotonic() - 999
    m._last_pass_at = time.monotonic() - 999
    assert await m.due(now) == "idle"

    app.settings.set(app.db, "knowledge.maintenance_enabled", False)
    assert await m.due(now) is None


async def test_queue_polling_tracks_the_idle_window(app):
    m = app.maintenance
    m.poll_queue()
    assert m.idle_for() is not None
    app.models.queue_state = lambda: {"depth": 2, "running": ["r1"]}
    m.poll_queue()
    assert m.idle_for() is None
    app.models.queue_state = lambda: {"depth": 0, "running": []}
    m.poll_queue()
    assert m.idle_for() is not None


async def test_has_work_sees_unconsolidated_events(app):
    assert await app.maintenance.has_work() is False
    events.append(app, app.desk_id(), "user", "hello")
    assert await app.maintenance.has_work() is True


async def test_curation_candidates_alone_do_not_trigger_a_pass(app):
    """A dangling link the model cannot honestly resolve must not re-trigger a pass every
    idle window; curation rides along on passes other work started."""
    app.vault.write("topics/x.md", "See [[ghost]] for details.", op="create")
    assert app.index.dangling()
    assert await app.maintenance.has_work() is False


async def test_the_lock_prevents_overlap(app):
    m = app.maintenance
    await m.lock.acquire()
    m.current = {"reason": "nightly", "started_at": "2026-09-20T03:00:00Z"}
    try:
        marker = await m.run("manual")
    finally:
        m.lock.release()
        m.current = None
    assert marker["in_progress"] is True
    assert marker["reason"] == "nightly"


async def test_status_shape(app):
    status = app.maintenance.status()
    assert status["enabled"] is True and status["running"] is False
    assert status["last_report"] is None
    assert status["next_nightly_at"].startswith("20")
    await app.maintenance.run("test")
    assert app.maintenance.status()["last_report"]["reason"] == "test"


async def test_progress_events_are_emitted(app, fake):
    seen = []
    sub = app.bus.subscribe()
    events.append(app, app.desk_id(), "user", "durable fact")
    fake.on("maintenance", responder(Scripted(content="nothing to record")))

    await app.maintenance.run("test")

    while not sub.queue.empty():
        event = sub.queue.get_nowait()
        if event["type"] == "maintenance.progress":
            seen.append(event["data"]["stage"])
    app.bus.unsubscribe(sub)
    assert "start" in seen and "consolidate" in seen and "done" in seen
