"""The filesystem guard (03 §9.3 step 3): roots, protected paths, one ask at a time."""

from __future__ import annotations

import os

import pytest

from loom import db
from loom.tools import fs_guard
from loom.tools.registry import Tool, ToolResult

WINDOWS = os.name == "nt"


async def _noop(ctx, args) -> ToolResult:  # pragma: no cover - never executed here
    return ToolResult("ok")


def _tool(name="fs_write", path_params=("path",)) -> Tool:
    return Tool(name=name, description="d", parameters={"type": "object"}, handler=_noop, path_params=path_params)


@pytest.fixture
def bus_events(app):
    """Every event emitted during the test, in order."""
    seen: list[dict] = []
    app.bus.listen(seen.append)
    return seen


@pytest.fixture
def project(app, tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('hi')", encoding="utf-8")
    fs_guard.add_root(app, str(root), added_by="test")
    return root


def _desk(app):
    return app.conversation(app.desk_id())


# -- normalise ---------------------------------------------------------------------
def test_normalise_is_absolute_forward_slashed_and_resolved(tmp_path):
    messy = str(tmp_path / "a" / ".." / "b" / "c") + "/"
    out = fs_guard.normalise(messy)
    assert out == (str(tmp_path / "b" / "c")).replace("\\", "/")
    assert "\\" not in out
    assert not out.endswith("/")


def test_normalise_preserves_case(tmp_path):
    out = fs_guard.normalise(str(tmp_path / "MixedCase" / "File.TXT"))
    assert out.endswith("MixedCase/File.TXT")


def test_normalise_resolves_relative_paths_against_cwd(tmp_path):
    out = fs_guard.normalise("src/main.py", cwd=str(tmp_path))
    assert out == (str(tmp_path / "src" / "main.py")).replace("\\", "/")


def test_normalise_strips_quotes_and_expands_user():
    assert fs_guard.normalise('"~"') == str(os.path.expanduser("~")).replace("\\", "/")


# -- roots -------------------------------------------------------------------------
def test_effective_roots_include_vault_and_artifacts(app):
    roots = fs_guard.effective_roots(app, _desk(app))
    assert fs_guard.normalise(str(app.vault_dir)) in roots
    assert fs_guard.normalise(str(app.artifacts_dir)) in roots


def test_effective_roots_include_the_conversation_own_roots(app, tmp_path):
    extra = tmp_path / "scratch"
    conversation = dict(_desk(app))
    conversation["roots"] = db.dumps([str(extra)])
    assert fs_guard.normalise(str(extra)) in fs_guard.effective_roots(app, conversation)


def test_add_root_is_idempotent_and_announces_itself(app, tmp_path, bus_events):
    stored = fs_guard.add_root(app, str(tmp_path / "x"), added_by="test")
    fs_guard.add_root(app, str(tmp_path / "x"), added_by="test")
    rows = app.db.execute("SELECT * FROM roots WHERE path=?", (stored,)).fetchall()
    assert len(rows) == 1
    assert any(e["type"] == "settings.changed" and e["data"]["keys"] == ["roots"] for e in bus_events)


def test_remove_root(app, tmp_path):
    stored = fs_guard.add_root(app, str(tmp_path / "gone"))
    assert fs_guard.remove_root(app, stored) is True
    assert fs_guard.remove_root(app, stored) is False


# -- check -------------------------------------------------------------------------
def test_a_path_inside_a_root_passes(app, project):
    guard = fs_guard.check(app, _desk(app), _tool(), {"path": str(project / "src" / "main.py")})
    assert isinstance(guard, fs_guard.GuardOk)


def test_a_path_outside_every_root_asks_and_offers_the_root_that_would_fix_it(app, tmp_path):
    target = tmp_path / "elsewhere" / "notes.txt"
    guard = fs_guard.check(app, _desk(app), _tool(), {"path": str(target)})
    assert isinstance(guard, fs_guard.GuardAsk)
    assert "outside allowed roots" in guard.reason
    assert guard.options["add_root"] == fs_guard.normalise(str(tmp_path / "elsewhere"))


def test_an_existing_directory_suggests_itself_as_the_root(app, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    guard = fs_guard.check(app, _desk(app), _tool(), {"path": str(target)})
    assert guard.options["add_root"] == fs_guard.normalise(str(target))


def test_a_protected_file_asks_even_inside_a_root(app, project):
    secret = project / "src" / "keys.txt"
    secret.write_text("x", encoding="utf-8")
    fs_guard.add_protected(app, str(secret))
    guard = fs_guard.check(app, _desk(app), _tool(), {"path": str(secret)})
    assert isinstance(guard, fs_guard.GuardAsk)
    assert "protected path" in guard.reason
    assert guard.options == {}


def test_a_protected_directory_covers_everything_under_it(app, project):
    fs_guard.add_protected(app, str(project / "src"))
    assert fs_guard.is_protected(app, str(project / "src" / "main.py")) is True
    assert fs_guard.is_protected(app, str(project / "README.md")) is False
    # a sibling with the same prefix is not "under" it
    assert fs_guard.is_protected(app, str(project / "srcx" / "a.py")) is False


def test_bootstrap_protected_rows_are_live(app):
    rows = [r["path"] for r in app.db.execute("SELECT path FROM protected_paths")]
    assert any(p.endswith("src/loom/tools/permissions.py") for p in rows)
    assert any(p.endswith("secrets.json") for p in rows)
    assert fs_guard.is_protected(app, str(app.home / "secrets.json")) is True


@pytest.mark.skipif(not WINDOWS, reason="Windows compares paths case-insensitively")
def test_windows_paths_compare_case_insensitively(app, project):
    weird = str(project).upper() + "/src/main.py"
    assert isinstance(fs_guard.check(app, _desk(app), _tool(), {"path": weird}), fs_guard.GuardOk)
    fs_guard.add_protected(app, str(project / "src" / "main.py"))
    assert fs_guard.is_protected(app, weird) is True


def test_relative_paths_resolve_against_cwd_then_the_first_root(app, project):
    tool = _tool(name="shell", path_params=("cwd", "path"))
    guard = fs_guard.check(app, _desk(app), tool, {"cwd": str(project), "path": "src/main.py"})
    assert isinstance(guard, fs_guard.GuardOk)
    assert guard.paths[-1].endswith("project/src/main.py")


def test_a_list_of_paths_is_checked_element_by_element(app, project, tmp_path):
    tool = _tool(name="fs_many", path_params=("paths",))
    inside = str(project / "src" / "main.py")
    outside = str(tmp_path / "other" / "x.txt")
    assert isinstance(fs_guard.check(app, _desk(app), tool, {"paths": [inside]}), fs_guard.GuardOk)
    assert isinstance(fs_guard.check(app, _desk(app), tool, {"paths": [inside, outside]}), fs_guard.GuardAsk)


def test_a_tool_without_path_params_is_never_guarded(app, tmp_path):
    tool = _tool(name="knowledge_write", path_params=())
    guard = fs_guard.check(app, _desk(app), tool, {"path": "core/me.md"})
    assert isinstance(guard, fs_guard.GuardOk)


def test_missing_and_empty_path_arguments_are_ignored(app):
    assert isinstance(fs_guard.check(app, _desk(app), _tool(), {}), fs_guard.GuardOk)
    assert isinstance(fs_guard.check(app, _desk(app), _tool(), {"path": "   "}), fs_guard.GuardOk)


def test_within_roots_helper(app, project, tmp_path):
    assert fs_guard.within_roots(app, _desk(app), str(project / "a.txt")) is True
    assert fs_guard.within_roots(app, _desk(app), str(tmp_path / "nope" / "a.txt")) is False


@pytest.mark.skipif(not WINDOWS, reason="MSYS drive spellings only mean something on Windows")
def test_normalise_reads_msys_and_cygwin_drive_paths():
    # Git Bash prints C:/Users as /c/Users; the model hands it straight back.
    assert fs_guard.normalise("/c/Users/x/file.md") == "C:/Users/x/file.md"
    assert fs_guard.normalise("/cygdrive/d/data") == "D:/data"
    assert fs_guard.normalise("/c") == "C:/"
    # a relative path that merely starts with a letter is untouched
    assert fs_guard.normalise("c/Users", cwd="D:/base") == "D:/base/c/Users"
