"""fs_write and fs_edit: the stale-write ledger, multi-edit, line endings, the vault.

The rules under test (05-backlog / tool-parity audit #2, #3, #18, TD audit F9):
    a write to a file this conversation has read, unchanged since → proceeds
    a write to a file that changed on disk                        → refused, read it again
    a write to a file nobody here has read                        → refused, read it first
    a write to a file that is not there                           → creates it
    several edits                                                 → in order, all or none
    CRLF and a BOM                                                → survive the write
    a relative path that names a vault note                       → knowledge_write
"""

from __future__ import annotations

import os

import pytest

from loom.tools import fs_guard
from loom.tools.native.fs import ledger
from loom.tools.registry import ToolContext


@pytest.fixture
def project(app, tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    fs_guard.add_root(app, str(root), added_by="test")
    return root


@pytest.fixture
def ctx(app, project):
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1", event_id=1)


@pytest.fixture
def other_ctx(app, project):
    """A second conversation: the ledger is per conversation, not per file."""
    from loom import db
    from loom.ids import ulid

    cid, now = ulid(), db.now_iso()
    app.db.execute(
        "INSERT INTO conversations(id, kind, title, role, status, created_at, updated_at) "
        "VALUES (?, 'thread', 'other', 'worker', 'idle', ?, ?)",
        (cid, now, now),
    )
    return ToolContext(app=app, conversation=app.conversation(cid), call_id="c2", event_id=2)


async def run(app, ctx, name, args):
    return await app.tools.get(name).handler(ctx, args)


def touch(path, content: str) -> None:
    """An edit by someone else: new content and a mtime that cannot collide."""
    path.write_text(content, encoding="utf-8")
    stamp = path.stat().st_mtime + 10
    os.utime(path, (stamp, stamp))


# ---------------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------------
async def test_fs_write_creates_a_new_file_without_a_read(app, ctx, project):
    target = project / "deep" / "new.txt"
    result = await run(app, ctx, "fs_write", {"path": str(target), "content": "hello"})
    assert result.ok and "wrote 5 bytes" in result.content
    assert target.read_text(encoding="utf-8") == "hello"
    assert result.meta["created"] is True


async def test_fs_write_refuses_a_file_this_conversation_has_not_read(app, ctx, project):
    target = project / "a.txt"
    target.write_text("theirs", encoding="utf-8")
    result = await run(app, ctx, "fs_write", {"path": str(target), "content": "mine"})
    assert not result.ok and "has not read it" in result.content
    assert target.read_text(encoding="utf-8") == "theirs"


async def test_fs_write_proceeds_after_a_read_and_refuses_after_a_change(app, ctx, project):
    target = project / "a.txt"
    target.write_text("one\n", encoding="utf-8")
    assert (await run(app, ctx, "fs_read", {"path": str(target)})).ok

    first = await run(app, ctx, "fs_write", {"path": str(target), "content": "two\n"})
    assert first.ok and target.read_text(encoding="utf-8") == "two\n"

    # the write recorded what it left behind, so a second write needs no second read
    again = await run(app, ctx, "fs_write", {"path": str(target), "content": "three\n"})
    assert again.ok

    touch(target, "somebody else was here\n")
    stale = await run(app, ctx, "fs_write", {"path": str(target), "content": "four\n"})
    assert not stale.ok and "changed on disk" in stale.content and "fs_read it again" in stale.content
    assert target.read_text(encoding="utf-8") == "somebody else was here\n"

    assert (await run(app, ctx, "fs_read", {"path": str(target)})).ok
    assert (await run(app, ctx, "fs_write", {"path": str(target), "content": "four\n"})).ok


async def test_the_ledger_is_per_conversation(app, ctx, other_ctx, project):
    target = project / "a.txt"
    target.write_text("one\n", encoding="utf-8")
    await run(app, ctx, "fs_read", {"path": str(target)})
    assert (await run(app, other_ctx, "fs_write", {"path": str(target), "content": "x"})).ok is False
    assert (await run(app, ctx, "fs_write", {"path": str(target), "content": "x"})).ok


async def test_the_ledger_marks_the_files_a_conversation_wrote(app, ctx, project):
    read_only = project / "read.txt"
    read_only.write_text("x", encoding="utf-8")
    await run(app, ctx, "fs_read", {"path": str(read_only)})
    await run(app, ctx, "fs_write", {"path": str(project / "written.txt"), "content": "y"})

    wrote = ledger.written(app, ctx.conversation_id)
    assert any(p.endswith("/written.txt") for p in wrote)
    assert not any(p.endswith("/read.txt") for p in wrote)


async def test_fs_edit_refuses_an_unread_file(app, ctx, project):
    target = project / "a.py"
    target.write_text("a = 1\n", encoding="utf-8")
    result = await run(app, ctx, "fs_edit", {"path": str(target), "old": "a = 1", "new": "a = 2"})
    assert not result.ok and "fs_read it first" in result.content
    assert target.read_text(encoding="utf-8") == "a = 1\n"


# ---------------------------------------------------------------------------------
# one edit and several
# ---------------------------------------------------------------------------------
async def test_fs_edit_still_takes_one_old_and_new(app, ctx, project):
    target = project / "a.py"
    target.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    await run(app, ctx, "fs_read", {"path": str(target)})
    result = await run(app, ctx, "fs_edit", {"path": str(target), "old": "b = 2", "new": "b = 22"})
    assert result.ok and "1 replacement" in result.content
    assert "-b = 2" in result.content and "+b = 22" in result.content
    assert target.read_text(encoding="utf-8") == "a = 1\nb = 22\nc = 3\n"


async def test_fs_edit_applies_several_edits_in_order_with_one_diff(app, ctx, project):
    target = project / "a.py"
    target.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    await run(app, ctx, "fs_read", {"path": str(target)})
    result = await run(
        app,
        ctx,
        "fs_edit",
        {
            "path": str(target),
            "edits": [
                {"old": "a = 1", "new": "a = 11"},
                {"old": "c = 3", "new": "c = 33"},
                {"old": "a = 11", "new": "a = 111"},  # sees the first edit's result
            ],
        },
    )
    assert result.ok and result.meta["replacements"] == 3 and result.meta["edits"] == 3
    assert target.read_text(encoding="utf-8") == "a = 111\nb = 2\nc = 33\n"
    assert result.content.count("@@") >= 1  # one diff, not three results


async def test_a_failing_edit_leaves_the_file_untouched(app, ctx, project):
    target = project / "a.py"
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")
    await run(app, ctx, "fs_read", {"path": str(target)})
    result = await run(
        app,
        ctx,
        "fs_edit",
        {"path": str(target), "edits": [{"old": "a = 1", "new": "a = 9"}, {"old": "nowhere", "new": "x"}]},
    )
    assert not result.ok
    assert "edit 2 of 2" in result.content and "not found" in result.content
    assert target.read_text(encoding="utf-8") == "a = 1\nb = 2\n"


async def test_multi_edit_keeps_the_uniqueness_rule_per_edit(app, ctx, project):
    target = project / "a.py"
    target.write_text("x\nx\ny\n", encoding="utf-8")
    await run(app, ctx, "fs_read", {"path": str(target)})
    clash = await run(app, ctx, "fs_edit", {"path": str(target), "edits": [{"old": "x", "new": "z"}]})
    assert not clash.ok and "appears 2 times" in clash.content

    ok = await run(
        app, ctx, "fs_edit", {"path": str(target), "edits": [{"old": "x", "new": "z", "replace_all": True}]}
    )
    assert ok.ok and target.read_text(encoding="utf-8") == "z\nz\ny\n"


async def test_fs_edit_rejects_both_forms_at_once_and_neither(app, ctx, project):
    target = project / "a.py"
    target.write_text("a\n", encoding="utf-8")
    await run(app, ctx, "fs_read", {"path": str(target)})
    both = await run(
        app, ctx, "fs_edit", {"path": str(target), "old": "a", "new": "b", "edits": [{"old": "a", "new": "b"}]}
    )
    assert not both.ok and "not both" in both.content
    neither = await run(app, ctx, "fs_edit", {"path": str(target)})
    assert not neither.ok and "nothing to do" in neither.content


async def test_fs_edit_says_when_the_text_is_not_there(app, ctx, project):
    target = project / "a.py"
    target.write_text("hello", encoding="utf-8")
    await run(app, ctx, "fs_read", {"path": str(target)})
    result = await run(app, ctx, "fs_edit", {"path": str(target), "old": "goodbye", "new": "x"})
    assert not result.ok and "not found" in result.content
    assert "line-number prefixes are not part of the text" in result.content


# ---------------------------------------------------------------------------------
# line endings and the BOM (audit #18)
# ---------------------------------------------------------------------------------
async def test_fs_edit_keeps_crlf_and_the_bom(app, ctx, project):
    target = project / "win.txt"
    target.write_bytes("﻿one\r\ntwo\r\nthree\r\n".encode("utf-8"))
    await run(app, ctx, "fs_read", {"path": str(target)})
    result = await run(app, ctx, "fs_edit", {"path": str(target), "old": "two", "new": "TWO"})
    assert result.ok
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\r\n") == 3 and b"TWO" in raw


async def test_fs_read_does_not_show_the_bom_it_preserves(app, ctx, project):
    """What fs_read shows is what fs_edit matches: the BOM is the file's encoding mark,
    so it is stripped on the way out and put back on the way in."""
    target = project / "win.txt"
    target.write_bytes("﻿one\r\ntwo\r\n".encode("utf-8"))
    shown = await run(app, ctx, "fs_read", {"path": str(target)})
    assert shown.content.startswith("1\tone")
    assert "﻿" not in shown.content

    assert (await run(app, ctx, "fs_edit", {"path": str(target), "old": "one", "new": "ONE"})).ok
    assert target.read_bytes().startswith(b"\xef\xbb\xbfONE")


async def test_fs_write_keeps_the_line_endings_of_the_file_it_overwrites(app, ctx, project):
    target = project / "win.txt"
    target.write_bytes(b"one\r\ntwo\r\n")
    await run(app, ctx, "fs_read", {"path": str(target)})
    result = await run(app, ctx, "fs_write", {"path": str(target), "content": "three\nfour\n"})
    assert result.ok and "CRLF" in result.content
    assert target.read_bytes() == b"three\r\nfour\r\n"


async def test_a_new_file_gets_plain_lf(app, ctx, project):
    target = project / "fresh.txt"
    assert (await run(app, ctx, "fs_write", {"path": str(target), "content": "a\nb\n"})).ok
    assert target.read_bytes() == b"a\nb\n"


async def test_a_file_with_mixed_endings_is_left_alone(app, ctx, project):
    target = project / "mixed.txt"
    target.write_bytes(b"one\r\ntwo\nthree\r\n")
    await run(app, ctx, "fs_read", {"path": str(target)})
    assert (await run(app, ctx, "fs_edit", {"path": str(target), "old": "two", "new": "TWO"})).ok
    assert target.read_bytes() == b"one\r\nTWO\nthree\r\n"


# ---------------------------------------------------------------------------------
# vault-shaped paths (TD audit F9)
# ---------------------------------------------------------------------------------
async def test_a_relative_path_that_names_a_vault_note_points_at_knowledge_write(app, ctx, project):
    app.vault.put_raw("projects/loom.md", "---\ntitle: Loom\n---\n\nnotes\n", actor="test")

    written = await run(app, ctx, "fs_write", {"path": "projects/loom.md", "content": "x"})
    assert not written.ok and "knowledge_write" in written.content and "vault note" in written.content
    assert not (project / "projects" / "loom.md").exists()

    edited = await run(app, ctx, "fs_edit", {"path": "projects/loom.md", "old": "notes", "new": "more"})
    assert not edited.ok and "knowledge_write" in edited.content


async def test_a_relative_path_that_is_not_a_note_still_says_what_it_resolved_against(app, ctx, project):
    result = await run(app, ctx, "fs_edit", {"path": "nope/missing.txt", "old": "a", "new": "b"})
    assert not result.ok and "relative paths resolve against" in result.content
