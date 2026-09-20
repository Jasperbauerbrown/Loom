"""Artifacts: spill, bound, paging recall, pruning (03 §4.4)."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from loom import db
from loom.artifacts import Store
from loom.ids import ulid


def _cap(app, value: int) -> None:
    app.settings.set(app.db, "conversation.tool_output_cap", value)


def test_install_attaches_a_store(app):
    assert isinstance(app.artifacts, Store)
    assert Path(app.artifacts_dir).is_dir()


def test_bound_leaves_short_output_alone(app):
    text, artifact_id = app.artifacts.bound(app.desk_id(), 1, "short output")
    assert text == "short output"
    assert artifact_id is None
    assert app.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_bound_spills_over_the_cap_with_head_tail_and_pointer(app):
    _cap(app, 500)
    app.settings.set(app.db, "conversation.artifact_head_chars", 20)
    app.settings.set(app.db, "conversation.artifact_tail_chars", 10)
    body = "A" * 400 + "B" * 400
    text, artifact_id = app.artifacts.bound(app.desk_id(), 7, body)

    assert artifact_id
    assert text.startswith("A" * 20)
    assert "…" in text
    assert f"[artifact {artifact_id}: 800 chars" in text
    assert "artifact_recall" in text

    row = app.artifacts.get(artifact_id)
    assert row["conversation_id"] == app.desk_id()
    assert row["event_id"] == 7
    assert row["bytes"] == 800
    assert Path(row["path"]).read_text(encoding="utf-8") == body


def test_two_spills_on_one_event_do_not_clobber_each_other(app):
    _cap(app, 500)
    first, id_a = app.artifacts.bound(app.desk_id(), 3, "x" * 600)
    second, id_b = app.artifacts.bound(app.desk_id(), 3, "y" * 600)
    assert id_a != id_b
    paths = {app.artifacts.get(id_a)["path"], app.artifacts.get(id_b)["path"]}
    assert len(paths) == 2
    assert app.artifacts.text(id_a) == "x" * 600
    assert app.artifacts.text(id_b) == "y" * 600


def test_read_pages_with_a_footer(app):
    row = app.artifacts.spill(app.desk_id(), None, "0123456789" * 10)
    chunk = app.artifacts.read(row["id"], offset=0, limit=30)
    assert chunk.startswith("012345678901234567890123456789")
    assert "chars 0–30 of 100" in chunk
    assert "offset=30" in chunk

    tail = app.artifacts.read(row["id"], offset=90, limit=30)
    assert tail == "0123456789"  # no footer: nothing remains


def test_read_reports_missing_artifact_and_past_the_end(app):
    assert app.artifacts.read("NOPE").startswith("error: no artifact")
    row = app.artifacts.spill(app.desk_id(), None, "abc")
    assert "past the end" in app.artifacts.read(row["id"], offset=99)


def test_read_reports_a_missing_file(app):
    row = app.artifacts.spill(app.desk_id(), None, "abc")
    Path(row["path"]).unlink()
    assert "file is missing" in app.artifacts.read(row["id"])


def test_list_by_conversation(app):
    desk = app.desk_id()
    a = app.artifacts.spill(desk, None, "one")
    b = app.artifacts.spill(desk, None, "two")
    ids = [r["id"] for r in app.artifacts.list(desk)]
    assert ids == sorted([a["id"], b["id"]])
    assert app.artifacts.list("other-conversation") == []


def test_prune_archived_removes_old_archived_artifacts_only(app):
    desk = app.desk_id()
    now = db.now_iso()
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=200)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    stale, fresh = ulid(), ulid()
    for cid, archived_at in ((stale, old), (fresh, now)):
        app.db.execute(
            "INSERT INTO conversations(id, kind, title, role, status, created_at, updated_at, archived_at) "
            "VALUES (?, 'thread', 't', 'worker', 'archived', ?, ?, ?)",
            (cid, now, now, archived_at),
        )
    rows = [
        app.artifacts.spill(stale, None, "gone"),
        app.artifacts.spill(fresh, None, "kept"),
        app.artifacts.spill(desk, None, "live"),
    ]

    removed = app.artifacts.prune_archived(90)

    assert removed == 1
    assert app.artifacts.get(rows[0]["id"]) is None
    assert not Path(rows[0]["path"]).exists()
    assert app.artifacts.get(rows[1]["id"]) is not None
    assert app.artifacts.get(rows[2]["id"]) is not None


def test_prune_uses_the_setting_by_default(app):
    assert app.artifacts.prune_archived() == 0
