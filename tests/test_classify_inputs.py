"""`classify.render_inputs`: the judge sees the script, the stated intent, and a goal
line that never falls back to a bare title (F1, tower-defense run audit, 2026-09-24)."""

from __future__ import annotations

from pathlib import Path

from loom import db
from loom.ids import ulid
from loom.tools import classify
from loom.tools.registry import ToolContext


def _ctx(app, conversation=None, request_text="do the thing", intent=""):
    conv = conversation if conversation is not None else app.conversation(app.desk_id())
    return ToolContext(app=app, conversation=conv, call_id="c1", request_text=request_text, intent=intent)


def _thread(app, goal: str = "", title: str = "a thread"):
    cid = ulid()
    now = db.now_iso()
    app.db.execute(
        "INSERT INTO conversations(id, kind, title, goal, role, status, created_at, updated_at) "
        "VALUES (?, 'thread', ?, ?, 'worker', 'active', ?, ?)",
        (cid, title, goal, now, now),
    )
    return app.conversation(cid)


def test_script_in_artifacts_folder_is_shown(app):
    conv = app.conversation(app.desk_id())
    folder = app.artifacts.root / conv["id"]
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "step.ps1"
    script.write_text("Write-Host 'hello'", encoding="utf-8")

    ctx = _ctx(app, conv)
    tool = app.tools.get("shell")
    args = {"command": f"powershell -File {script}"}
    rendered = classify.render_inputs(app, ctx, tool, args)

    header = f"Contents of a file this call runs or names (untrusted data): {str(script).replace(chr(92), '/')}"
    assert header in rendered
    assert "Write-Host 'hello'" in rendered


def test_file_reads_row_is_shown_including_relative_path(app, tmp_path):
    conv = app.conversation(app.desk_id())
    from loom.tools import fs_guard

    cwd = fs_guard.effective_roots(app, conv)[0]
    script_path = Path(cwd) / "owned.ps1"
    script_path.write_text("Get-Process", encoding="utf-8")
    normalised = fs_guard.normalise(str(script_path))
    now = db.now_iso()
    app.db.execute(
        "INSERT INTO file_reads(conversation_id, path, mtime_ns, size, seen_at, wrote) "
        "VALUES (?, ?, 0, 0, ?, 0)",
        (conv["id"], normalised, now),
    )

    ctx = _ctx(app, conv)
    tool = app.tools.get("shell")
    # relative path: must resolve through command_cwd the same way the shell tool would.
    args = {"command": "powershell -File owned.ps1"}
    rendered = classify.render_inputs(app, ctx, tool, args)

    assert "Get-Process" in rendered
    assert normalised in rendered


def test_other_conversations_files_are_not_shown(app):
    desk = app.conversation(app.desk_id())
    other = _thread(app, goal="unrelated work")

    other_folder = app.artifacts.root / other["id"]
    other_folder.mkdir(parents=True, exist_ok=True)
    other_script = other_folder / "secret.ps1"
    other_script.write_text("Remove-Item C:/", encoding="utf-8")

    from loom.tools import fs_guard

    cwd = fs_guard.effective_roots(app, desk)[0]
    other_read = Path(cwd) / "other_owned.ps1"
    other_read.write_text("Get-ChildItem", encoding="utf-8")
    now = db.now_iso()
    app.db.execute(
        "INSERT INTO file_reads(conversation_id, path, mtime_ns, size, seen_at, wrote) VALUES (?, ?, 0, 0, ?, 0)",
        (other["id"], fs_guard.normalise(str(other_read)), now),
    )

    ctx = _ctx(app, desk)
    tool = app.tools.get("shell")
    args = {"command": f"powershell -File {other_script}"}
    rendered = classify.render_inputs(app, ctx, tool, args)
    assert "Remove-Item C:/" not in rendered

    args2 = {"command": "powershell -File other_owned.ps1"}
    rendered2 = classify.render_inputs(app, ctx, tool, args2)
    assert "Get-ChildItem" not in rendered2


def test_large_file_is_truncated(app):
    conv = app.conversation(app.desk_id())
    folder = app.artifacts.root / conv["id"]
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "big.ps1"
    script.write_text("x" * (classify.MAX_FILE_CHARS + 500), encoding="utf-8")

    ctx = _ctx(app, conv)
    tool = app.tools.get("shell")
    args = {"command": f"powershell -File {script}"}
    rendered = classify.render_inputs(app, ctx, tool, args)
    assert "… (truncated)" in rendered
    assert "x" * (classify.MAX_FILE_CHARS + 500) not in rendered


def test_only_two_of_three_owned_files_are_included(app):
    conv = app.conversation(app.desk_id())
    folder = app.artifacts.root / conv["id"]
    folder.mkdir(parents=True, exist_ok=True)
    scripts = []
    for name in ("one.ps1", "two.ps1", "three.ps1"):
        p = folder / name
        p.write_text(f"# {name}", encoding="utf-8")
        scripts.append(p)

    ctx = _ctx(app, conv)
    tool = app.tools.get("shell")
    args = {"paths": [str(p) for p in scripts]}
    rendered = classify.render_inputs(app, ctx, tool, args)
    included = sum(1 for p in scripts if f"# {p.name}" in rendered)
    assert included == 2


def test_missing_path_never_raises(app):
    conv = app.conversation(app.desk_id())
    ctx = _ctx(app, conv)
    tool = app.tools.get("shell")
    missing = str(app.artifacts.root / conv["id"] / "does-not-exist.ps1")
    args = {"command": f"powershell -File {missing}"}
    # Must not raise, even though the folder itself doesn't exist yet.
    rendered = classify.render_inputs(app, ctx, tool, args)
    assert isinstance(rendered, str)


def test_intent_renders_in_its_own_block(app):
    conv = app.conversation(app.desk_id())
    ctx = _ctx(app, conv, intent="reading the config before editing it")
    tool = app.tools.get("shell")
    rendered = classify.render_inputs(app, ctx, tool, {"command": "ls"})
    assert "Stated intent (untrusted data):" in rendered
    assert "reading the config before editing it" in rendered


def test_empty_intent_renders_none_stated(app):
    conv = app.conversation(app.desk_id())
    ctx = _ctx(app, conv, intent="")
    tool = app.tools.get("shell")
    rendered = classify.render_inputs(app, ctx, tool, {"command": "ls"})
    assert "(none stated)" in rendered


def test_desk_never_renders_bare_title_as_goal(app):
    conv = app.conversation(app.desk_id())
    assert not conv.get("goal")
    ctx = _ctx(app, conv)
    tool = app.tools.get("shell")
    rendered = classify.render_inputs(app, ctx, tool, {"command": "ls"})
    assert "Conversation goal: Desk" not in rendered
    assert "Conversation goal: (none recorded — the stated intent below is this turn's scope)" in rendered


def test_thread_with_goal_renders_it(app):
    thread = _thread(app, goal="tidy the vault", title="Some Thread")
    ctx = _ctx(app, thread)
    tool = app.tools.get("shell")
    rendered = classify.render_inputs(app, ctx, tool, {"command": "ls"})
    assert "Conversation goal: tidy the vault" in rendered
