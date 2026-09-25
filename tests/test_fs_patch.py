"""fs_patch: one unified diff across files (parity audit #3).

Covers the parser, the exact-match hunk rule, create/delete/rename, all-or-nothing,
`dry_run`, the stale-write ledger per file, and the guard reading the paths *inside* the
diff (`fs_guard.register_paths`) rather than from an argument.
"""

from __future__ import annotations

import pytest

from loom.tools import fs_guard
from loom.tools.native.fs import patch as patch_mod
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


async def run(app, ctx, name, args):
    return await app.tools.get(name).handler(ctx, args)


async def seed(app, ctx, path, text: str) -> None:
    """A file this conversation has read, which is what the ledger requires."""
    path.write_text(text, encoding="utf-8")
    assert (await run(app, ctx, "fs_read", {"path": str(path)})).ok


UPDATE_A = """\
--- a/a.txt
+++ b/a.txt
@@ -1,3 +1,3 @@
 one
-two
+TWO
 three
"""

UPDATE_B = """\
--- a/b.txt
+++ b/b.txt
@@ -1,2 +1,3 @@
 alpha
+beta
 gamma
"""


# ---------------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------------
def test_parse_reads_file_sections_hunks_and_paths():
    files = patch_mod.parse(UPDATE_A + UPDATE_B)
    assert [f.path for f in files] == ["a.txt", "b.txt"]
    assert [f.action for f in files] == ["update", "update"]
    assert files[0].hunks[0].old == ["one", "two", "three"]
    assert files[0].hunks[0].new == ["one", "TWO", "three"]
    assert files[0].plus == 1 and files[0].minus == 1


def test_parse_reads_git_style_noise_and_dev_null():
    text = (
        "diff --git a/new.txt b/new.txt\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "--- /dev/null\n"
        "+++ b/new.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+alpha\n"
        "+beta\n"
    )
    [file] = patch_mod.parse(text)
    assert file.action == "create" and file.path == "new.txt" and file.source is None


def test_parse_refuses_something_that_is_not_a_patch():
    with pytest.raises(patch_mod.PatchError) as exc:
        patch_mod.parse("please change two to TWO in a.txt")
    assert "no file sections" in str(exc.value)


def test_strip_drops_components_when_asked():
    text = "--- x/y/a.txt\n+++ x/y/a.txt\n@@ -1 +1 @@\n-a\n+b\n"
    assert patch_mod.parse(text)[0].path == "x/y/a.txt"
    assert patch_mod.parse(text, strip=2)[0].path == "a.txt"


# ---------------------------------------------------------------------------------
# the guard reads the paths inside the diff
# ---------------------------------------------------------------------------------
def test_the_guard_judges_paths_inside_the_diff(app, project, tmp_path):
    app.tools.get("fs_patch")  # registering the tools registers the extractor
    outside = (tmp_path / "elsewhere" / "x.txt").as_posix()
    text = f"--- a/{outside}\n+++ b/{outside}\n@@ -1 +1 @@\n-a\n+b\n"
    verdict = fs_guard.check(app, app.conversation(app.desk_id()), app.tools.get("fs_patch"), {"patch": text, "cwd": str(project)})
    assert isinstance(verdict, fs_guard.GuardAsk)
    assert "outside allowed roots" in verdict.reason and verdict.options["add_root"].endswith("elsewhere")


def test_the_guard_passes_a_patch_inside_the_roots(app, project):
    verdict = fs_guard.check(app, app.conversation(app.desk_id()), app.tools.get("fs_patch"), {"patch": UPDATE_A, "cwd": str(project)})
    assert isinstance(verdict, fs_guard.GuardOk)
    assert any(p.endswith("/a.txt") for p in verdict.paths)


def test_a_patch_that_will_not_parse_names_no_paths():
    assert patch_mod.patch_paths({"patch": "not a patch"}) == []


# ---------------------------------------------------------------------------------
# applying
# ---------------------------------------------------------------------------------
async def test_fs_patch_applies_across_files_in_one_call(app, ctx, project):
    await seed(app, ctx, project / "a.txt", "one\ntwo\nthree\n")
    await seed(app, ctx, project / "b.txt", "alpha\ngamma\n")

    result = await run(app, ctx, "fs_patch", {"patch": UPDATE_A + UPDATE_B, "cwd": str(project)})
    assert result.ok, result.content
    assert result.meta["files"] == 2 and result.meta["hunks"] == 2
    assert (project / "a.txt").read_text(encoding="utf-8") == "one\nTWO\nthree\n"
    assert (project / "b.txt").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"
    assert "updated a.txt" in result.content or "updated" in result.content


async def test_fs_patch_creates_deletes_and_renames(app, ctx, project):
    await seed(app, ctx, project / "gone.txt", "x\ny\n")
    await seed(app, ctx, project / "old.txt", "hello\n")

    created = await run(
        app,
        ctx,
        "fs_patch",
        {"patch": "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+alpha\n+beta\n", "cwd": str(project)},
    )
    assert created.ok and (project / "new.txt").read_text(encoding="utf-8") == "alpha\nbeta\n"

    deleted = await run(
        app, ctx, "fs_patch", {"patch": "--- a/gone.txt\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n", "cwd": str(project)}
    )
    assert deleted.ok and not (project / "gone.txt").exists()

    renamed = await run(
        app,
        ctx,
        "fs_patch",
        {"patch": "--- a/old.txt\n+++ b/newer.txt\n@@ -1 +1 @@\n-hello\n+hello there\n", "cwd": str(project)},
    )
    assert renamed.ok, renamed.content
    assert not (project / "old.txt").exists()
    assert (project / "newer.txt").read_text(encoding="utf-8") == "hello there\n"


async def test_a_hunk_is_matched_by_its_text_not_its_line_number(app, ctx, project):
    await seed(app, ctx, project / "a.txt", "pad\npad\npad\none\ntwo\nthree\n")
    result = await run(app, ctx, "fs_patch", {"patch": UPDATE_A, "cwd": str(project)})  # says line 1, sits at line 4
    assert result.ok, result.content
    assert (project / "a.txt").read_text(encoding="utf-8") == "pad\npad\npad\none\nTWO\nthree\n"


async def test_an_ambiguous_hunk_is_refused(app, ctx, project):
    await seed(app, ctx, project / "a.txt", "one\ntwo\nthree\none\ntwo\nthree\n")
    text = "--- a/a.txt\n+++ b/a.txt\n@@ -9,3 +9,3 @@\n one\n-two\n+TWO\n three\n"
    result = await run(app, ctx, "fs_patch", {"patch": text, "cwd": str(project)})
    assert not result.ok and "matches 2 places" in result.content
    assert "two" in (project / "a.txt").read_text(encoding="utf-8")


async def test_a_hunk_that_does_not_match_leaves_every_file_alone(app, ctx, project):
    await seed(app, ctx, project / "a.txt", "one\ntwo\nthree\n")
    await seed(app, ctx, project / "b.txt", "alpha\ngamma\n")
    bad = "--- a/b.txt\n+++ b/b.txt\n@@ -1,2 +1,2 @@\n alpha\n-delta\n+DELTA\n"

    result = await run(app, ctx, "fs_patch", {"patch": UPDATE_A + bad, "cwd": str(project)})
    assert not result.ok and "does not match" in result.content
    assert (project / "a.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\n"
    assert (project / "b.txt").read_text(encoding="utf-8") == "alpha\ngamma\n"


async def test_dry_run_reports_without_writing(app, ctx, project):
    await seed(app, ctx, project / "a.txt", "one\ntwo\nthree\n")
    result = await run(app, ctx, "fs_patch", {"patch": UPDATE_A, "cwd": str(project), "dry_run": True})
    assert result.ok and result.meta["dry_run"] is True and "dry run" in result.content
    assert (project / "a.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\n"


async def test_fs_patch_refuses_a_file_this_conversation_has_not_read(app, ctx, project):
    (project / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    result = await run(app, ctx, "fs_patch", {"patch": UPDATE_A, "cwd": str(project)})
    assert not result.ok and "fs_read it first" in result.content
    assert (project / "a.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\n"


async def test_fs_patch_refuses_a_file_that_changed_on_disk(app, ctx, project):
    import os

    target = project / "a.txt"
    await seed(app, ctx, target, "one\ntwo\nthree\n")
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    stamp = target.stat().st_mtime + 10
    os.utime(target, (stamp, stamp))

    result = await run(app, ctx, "fs_patch", {"patch": UPDATE_A, "cwd": str(project)})
    assert not result.ok and "changed on disk" in result.content


async def test_fs_patch_records_what_it_wrote(app, ctx, project):
    from loom.tools.native.fs import ledger

    await seed(app, ctx, project / "a.txt", "one\ntwo\nthree\n")
    assert (await run(app, ctx, "fs_patch", {"patch": UPDATE_A, "cwd": str(project)})).ok
    assert any(p.endswith("/a.txt") for p in ledger.written(app, ctx.conversation_id))
    # and a second patch needs no second read
    second = "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n one\n-TWO\n+2\n three\n"
    assert (await run(app, ctx, "fs_patch", {"patch": second, "cwd": str(project)})).ok


async def test_fs_patch_resolves_relative_paths_against_cwd(app, ctx, project):
    sub = project / "sub"
    sub.mkdir()
    await seed(app, ctx, sub / "a.txt", "one\ntwo\nthree\n")
    result = await run(app, ctx, "fs_patch", {"patch": UPDATE_A, "cwd": str(sub)})
    assert result.ok, result.content
    assert (sub / "a.txt").read_text(encoding="utf-8") == "one\nTWO\nthree\n"


async def test_fs_patch_keeps_crlf_and_the_bom(app, ctx, project):
    target = project / "win.txt"
    target.write_bytes("﻿one\r\ntwo\r\nthree\r\n".encode("utf-8"))
    assert (await run(app, ctx, "fs_read", {"path": str(target)})).ok
    text = "--- a/win.txt\n+++ b/win.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"
    assert (await run(app, ctx, "fs_patch", {"patch": text, "cwd": str(project)})).ok
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and raw.count(b"\r\n") == 3 and b"TWO" in raw


async def test_creating_a_file_that_is_already_there_is_refused(app, ctx, project):
    (project / "new.txt").write_text("mine\n", encoding="utf-8")
    result = await run(
        app, ctx, "fs_patch", {"patch": "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+alpha\n", "cwd": str(project)}
    )
    assert not result.ok and "already exists" in result.content
    assert (project / "new.txt").read_text(encoding="utf-8") == "mine\n"
