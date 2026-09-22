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


# -- paged tools are never spilled (2026-09-22) ------------------------------------------
# A recall page plus its footer used to exceed the cap and was spilled into a *new*
# artifact, so a model could never read past the first page of anything.

def _ctx(app, event_id: int = 2):
    from loom.tools.registry import ToolContext

    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1", event_id=event_id)


def _artifact_count(app) -> int:
    return int(app.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])


def _artifact_files(app) -> int:
    return sum(1 for p in Path(app.artifacts_dir).rglob("*.txt"))


async def test_recall_of_an_over_cap_artifact_is_never_spilled(app):
    from loom.tools import dispatch

    _cap(app, 500)
    row = app.artifacts.spill(app.desk_id(), 1, "x" * 1200)
    rows, files = _artifact_count(app), _artifact_files(app)
    tool = app.tools.get("artifact_recall")
    assert tool is not None and tool.paged

    first = await dispatch.execute(app, _ctx(app), tool, {"id": row["id"]})
    assert first.ok
    assert first.content.startswith("x" * 500)
    assert "chars 0–500 of 1200" in first.content and "offset=500" in first.content
    assert "artifact_id" not in first.meta
    assert (_artifact_count(app), _artifact_files(app)) == (rows, files)

    # the footer's offset is exact and the pages reach the end
    second = await dispatch.execute(app, _ctx(app), tool, {"id": row["id"], "offset": 500})
    assert "chars 500–1000 of 1200" in second.content
    last = await dispatch.execute(app, _ctx(app), tool, {"id": row["id"], "offset": 1000})
    assert last.content == "x" * 200
    assert (_artifact_count(app), _artifact_files(app)) == (rows, files)


def test_recall_limit_defaults_to_and_is_clamped_by_the_cap(app):
    _cap(app, 500)
    row = app.artifacts.spill(app.desk_id(), None, "y" * 1200)
    by_default = app.artifacts.read(row["id"])
    assert by_default.startswith("y" * 500) and "chars 0–500 of 1200" in by_default
    clamped = app.artifacts.read(row["id"], limit=100_000)
    assert clamped == by_default
    smaller = app.artifacts.read(row["id"], limit=100)
    assert smaller.startswith("y" * 100) and "chars 0–100 of 1200" in smaller


async def test_fs_read_over_the_cap_returns_one_page_and_is_never_spilled(app, tmp_path):
    from loom.tools import dispatch

    _cap(app, 500)
    target = tmp_path / "big.txt"
    target.write_text("\n".join(f"line {i:04d} {'-' * 12}" for i in range(200)), encoding="utf-8")
    rows = _artifact_count(app)
    tool = app.tools.get("fs_read")
    assert tool is not None and tool.paged

    first = await dispatch.execute(app, _ctx(app), tool, {"path": str(target)})
    assert first.ok and "artifact_id" not in first.meta
    body, footer = first.content.rsplit("\n", 1)
    assert len(body) <= 500
    assert footer.startswith("[lines 1–") and "of 200; fs_read(path, offset=" in footer
    assert _artifact_count(app) == rows

    end = int(footer.split("offset=")[1].split(")")[0])
    second = await dispatch.execute(app, _ctx(app), tool, {"path": str(target), "offset": end})
    assert second.ok and second.content.startswith(f"line {end:04d} ")
    assert _artifact_count(app) == rows

    # No line is lost between pages: the cut lands mid-line, and that line must open
    # the next page whole (its tail used to be skipped silently, 2026-09-22).
    pages, offset = [], 0
    while True:
        res = await dispatch.execute(app, _ctx(app), tool, {"path": str(target), "offset": offset})
        assert res.ok
        if "fs_read(path, offset=" not in res.content:
            pages.append(res.content)
            break
        body, footer = res.content.rsplit("\n", 1)
        pages.append(body)
        offset = int(footer.split("offset=")[1].split(")")[0])
    assert "\n".join(pages) == target.read_text(encoding="utf-8")


async def test_a_tool_that_does_not_page_still_spills(app):
    from loom.tools import dispatch
    from loom.tools.registry import Tool, ToolResult

    _cap(app, 500)
    app.settings.set(app.db, "conversation.artifact_head_chars", 20)
    app.settings.set(app.db, "conversation.artifact_tail_chars", 10)

    async def handler(ctx, args):
        return ToolResult(content="z" * 1200)

    tool = Tool(name="blurt", description="blurt", parameters={"type": "object", "properties": {}}, handler=handler)
    app.tools.register(tool, replace=True)
    rows = _artifact_count(app)
    result = await dispatch.execute(app, _ctx(app), tool, {})
    assert result.meta.get("artifact_id")
    assert "artifact_recall" in result.content and len(result.content) < 1200
    assert _artifact_count(app) == rows + 1
