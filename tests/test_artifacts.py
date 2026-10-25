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

    removed = app.artifacts.prune()

    assert removed["artifacts"] == 1
    assert app.artifacts.get(rows[0]["id"]) is None
    assert not Path(rows[0]["path"]).exists()
    assert not Path(rows[0]["path"]).parent.exists()  # the whole folder goes
    assert app.artifacts.get(rows[1]["id"]) is not None
    assert app.artifacts.get(rows[2]["id"]) is not None


def test_prune_with_nothing_to_do_reports_zeroes(app):
    assert app.artifacts.prune() == {"artifacts": 0, "files": 0, "bytes": 0, "failed": 0, "audio": 0}


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
    assert second.ok and second.content.startswith(f"{end + 1}\tline {end:04d} ")
    assert _artifact_count(app) == rows

    # No line is lost between pages: the cut lands mid-line, and that line must open
    # the next page whole (its tail used to be skipped silently, 2026-09-22).
    pages, offset = [], 0
    while True:
        res = await dispatch.execute(app, _ctx(app), tool, {"path": str(target), "offset": offset, "line_numbers": False})
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


# -- spawn-tree scoping (2026-09-24) -------------------------------------------------
# artifact_recall reads any conversation's artifact today; evidence should flow up the
# spawn tree (desk <- thread <- sub-thread) but never sideways or downward.

def _make_tree(app):
    """desk -> thread -> sub_thread, plus an unrelated sibling of thread."""
    from loom import conversation

    desk = app.desk_id()
    thread = conversation.create_thread(app, goal="do the work", parent_id=desk)["id"]
    sub = conversation.create_thread(app, goal="sub work", parent_id=thread)["id"]
    sibling = conversation.create_thread(app, goal="unrelated", parent_id=desk)["id"]
    return desk, thread, sub, sibling


def test_visible_ids_on_a_leaf_returns_just_itself(app):
    desk, thread, sub, sibling = _make_tree(app)
    assert app.artifacts.visible_ids(sub) == [sub]


def test_visible_ids_includes_the_whole_subtree(app):
    desk, thread, sub, sibling = _make_tree(app)
    ids = set(app.artifacts.visible_ids(thread))
    assert ids == {thread, sub}
    assert sibling not in ids
    assert desk not in ids


def test_read_in_tree_reaches_child_and_grandchild_not_sibling_or_parent(app):
    # sibling and thread are both direct children of desk, so they are true siblings of
    # each other (neither is in the other's spawn tree); desk is thread's parent.
    desk, thread, sub, sibling = _make_tree(app)
    child_row = app.artifacts.spill(thread, None, "child text")
    grandchild_row = app.artifacts.spill(sub, None, "grandchild text")
    sibling_row = app.artifacts.spill(sibling, None, "sibling text")
    desk_row = app.artifacts.spill(desk, None, "desk text")

    assert app.artifacts.read_in_tree(desk, child_row["id"]) == "child text"
    assert app.artifacts.read_in_tree(desk, grandchild_row["id"]) == "grandchild text"

    expected_sibling_error = f"error: no artifact {sibling_row['id']} in this conversation or a thread it spawned"
    expected_desk_error = f"error: no artifact {desk_row['id']} in this conversation or a thread it spawned"
    # thread cannot read its sibling's artifact...
    assert app.artifacts.read_in_tree(thread, sibling_row["id"]) == expected_sibling_error
    # ...and cannot read its own parent's artifact (evidence flows up, not down)
    assert app.artifacts.read_in_tree(thread, desk_row["id"]) == expected_desk_error

    # a non-existent id gets the exact same message shape
    assert app.artifacts.read_in_tree(desk, "NOPE") == "error: no artifact NOPE in this conversation or a thread it spawned"


def test_find_image_in_tree_scopes_the_same_way(app):
    desk, thread, sub, sibling = _make_tree(app)
    child_img = app.artifacts.store_image(thread, b"\x89PNG\r\n\x1a\n" + b"x" * 40, "image/png")
    sibling_img = app.artifacts.store_image(sibling, b"\x89PNG\r\n\x1a\n" + b"y" * 40, "image/png")

    found = app.artifacts.find_image_in_tree(desk, child_img["id"])
    assert found is not None
    path, mime = found
    assert mime == "image/png" and path.is_file()

    # sibling is not below thread in the spawn tree
    assert app.artifacts.find_image_in_tree(thread, sibling_img["id"]) is None
    assert app.artifacts.find_image_in_tree(desk, "img_not_a_real_id") is None


def _ctx_for(app, conversation_id: str, event_id: int = 1):
    from loom.tools.registry import ToolContext

    return ToolContext(app=app, conversation=app.conversation(conversation_id), call_id="c1", event_id=event_id)


async def test_artifact_recall_handler_scopes_text_through_the_tree(app):
    from loom.tools import dispatch

    desk, thread, sub, sibling = _make_tree(app)
    grandchild_row = app.artifacts.spill(sub, None, "grandchild text " * 5)
    sibling_row = app.artifacts.spill(sibling, None, "sibling text")

    tool = app.tools.get("artifact_recall")
    # desk spawned thread which spawned sub: desk can read the grandchild's artifact.
    ok = await dispatch.execute(app, _ctx_for(app, desk, 1), tool, {"id": grandchild_row["id"]})
    assert ok.ok and ok.content.startswith("grandchild text")

    # thread cannot read its sibling's artifact.
    refused = await dispatch.execute(app, _ctx_for(app, thread, 2), tool, {"id": sibling_row["id"]})
    assert not refused.ok
    assert refused.content == (
        f"error: no artifact {sibling_row['id']} in this conversation or a thread it spawned"
    )


async def test_artifact_recall_handler_returns_images_scoped_to_the_tree(app):
    from loom.tools import dispatch

    desk, thread, sub, sibling = _make_tree(app)
    png_bytes = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 32)
    child_img = app.artifacts.store_image(thread, png_bytes, "image/png")
    sibling_img = app.artifacts.store_image(sibling, png_bytes, "image/png")

    tool = app.tools.get("artifact_recall")

    ok = await dispatch.execute(app, _ctx_for(app, desk, 3), tool, {"id": child_img["id"]})
    assert ok.ok
    assert ok.meta.get("images")  # dispatch._store_images turns result.images into refs, keyed by conversation

    # the handler itself must have produced the raw image bytes before dispatch stores
    # them under the *calling* conversation; check that directly, bypassing dispatch.
    from loom.tools.native.misc import artifact_recall_handler

    direct = await artifact_recall_handler(_ctx_for(app, desk, 99), {"id": child_img["id"]})
    assert direct.ok
    assert direct.images == [{"data": png_bytes, "mime": "image/png"}]

    refused = await artifact_recall_handler(_ctx_for(app, thread, 100), {"id": sibling_img["id"]})
    assert not refused.ok
    assert refused.content == f"error: no image {sibling_img['id']} in this conversation or a thread it spawned"


async def test_artifact_recall_handler_reports_bad_ids_without_raising(app):
    from loom.tools.native.misc import artifact_recall_handler
    from loom.tools.registry import ToolContext

    ctx = ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c-bad", event_id=1)

    missing_text = await artifact_recall_handler(ctx, {"id": "NOPE"})
    assert not missing_text.ok and missing_text.content.startswith("error: no artifact NOPE")

    missing_image = await artifact_recall_handler(ctx, {"id": "img_not_a_real_id"})
    assert not missing_image.ok
    assert "img_not_a_real_id" in missing_image.content

    row = app.artifacts.spill(app.desk_id(), None, "hi")
    Path(row["path"]).unlink()
    missing_file = await artifact_recall_handler(ctx, {"id": row["id"]})
    assert not missing_file.ok and "file is missing" in missing_file.content
