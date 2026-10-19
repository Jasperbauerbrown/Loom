"""`classify.render_inputs`: the judge sees the script, the stated intent, the user's own
desk request and nothing a thread's goal or handoff says (F1, tower-defense run audit,
2026-09-24; printer threads, 2026-10-12)."""

from __future__ import annotations

from pathlib import Path

from loom import db
from loom.ids import ulid
from loom.tools import classify
from loom.tools.registry import ToolContext


def _ctx(app, conversation=None, request_text="do the thing", intent="", intent_carried=False):
    conv = conversation if conversation is not None else app.conversation(app.desk_id())
    return ToolContext(app=app, conversation=conv, call_id="c1", request_text=request_text, intent=intent,
                       intent_carried=intent_carried)


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


def test_script_inside_the_roots_is_shown_even_untouched(app):
    """A vault procedure helper the thread runs by path is judged by its contents, not
    its name (2026-10-12) — including another conversation's scratch script."""
    desk = app.conversation(app.desk_id())
    helper = Path(app.vault_dir) / "procedures" / "x" / "scripts" / "flatten_profiles.py"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("print('flatten')", encoding="utf-8")
    other = _thread(app, goal="unrelated work")
    other_folder = app.artifacts.root / other["id"]
    other_folder.mkdir(parents=True, exist_ok=True)
    other_script = other_folder / "step.ps1"
    other_script.write_text("Get-Date", encoding="utf-8")

    tool = app.tools.get("shell")
    rendered = classify.render_inputs(app, _ctx(app, desk), tool, {"command": f"python {helper} --help"})
    assert "print('flatten')" in rendered
    rendered2 = classify.render_inputs(app, _ctx(app, desk), tool, {"command": f"powershell -File {other_script}"})
    assert "Get-Date" in rendered2


def test_untouched_non_script_or_outside_roots_is_not_shown(app, tmp_path):
    """Untouched files are shown only when they are scripts inside the roots: never a
    `.env` beside them, never a file outside the roots another conversation read."""
    from loom.tools import fs_guard

    desk = app.conversation(app.desk_id())
    other = _thread(app, goal="unrelated work")
    cwd = fs_guard.effective_roots(app, desk)[0]
    env = Path(cwd) / ".env"
    env.write_text("TOKEN=hunter2", encoding="utf-8")
    outside = tmp_path / "elsewhere" / "other_owned.ps1"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("Get-ChildItem", encoding="utf-8")
    app.db.execute(
        "INSERT INTO file_reads(conversation_id, path, mtime_ns, size, seen_at, wrote) VALUES (?, ?, 0, 0, ?, 0)",
        (other["id"], fs_guard.normalise(str(outside)), db.now_iso()),
    )

    tool = app.tools.get("shell")
    command = f"source .env && powershell -File {outside}"
    rendered = classify.render_inputs(app, _ctx(app, desk), tool, {"command": command})
    assert "hunter2" not in rendered
    assert "Get-ChildItem" not in rendered


def test_protected_file_is_never_shown(app):
    """Not even one this conversation wrote."""
    from loom.tools import fs_guard

    conv = app.conversation(app.desk_id())
    folder = app.artifacts.root / conv["id"]
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "guarded.py"
    script.write_text("SECRET_BODY = 1", encoding="utf-8")
    app.db.execute(
        "INSERT INTO file_reads(conversation_id, path, mtime_ns, size, seen_at, wrote) VALUES (?, ?, 0, 0, ?, 1)",
        (conv["id"], fs_guard.normalise(str(script)), db.now_iso()),
    )
    fs_guard.add_protected(app, str(script))
    rendered = classify.render_inputs(app, _ctx(app, conv), app.tools.get("shell"), {"command": f"python {script}"})
    assert "SECRET_BODY" not in rendered


def test_written_files_outrank_scripts_in_the_roots(app):
    from loom.tools import fs_guard

    conv = app.conversation(app.desk_id())
    scripts_dir = Path(app.vault_dir) / "procedures" / "y" / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    plain = [scripts_dir / "a.py", scripts_dir / "b.py"]
    for p in plain:
        p.write_text(f"# plain {p.name}", encoding="utf-8")
    cwd = fs_guard.effective_roots(app, conv)[0]
    written = Path(cwd) / "mine.py"
    written.write_text("# written by me", encoding="utf-8")
    app.db.execute(
        "INSERT INTO file_reads(conversation_id, path, mtime_ns, size, seen_at, wrote) VALUES (?, ?, 0, 0, ?, 1)",
        (conv["id"], fs_guard.normalise(str(written)), db.now_iso()),
    )
    command = f"python {plain[0]} && python {plain[1]} && python {written}"
    rendered = classify.render_inputs(app, _ctx(app, conv), app.tools.get("shell"), {"command": command})
    assert "# written by me" in rendered
    assert "# plain a.py" in rendered
    assert "# plain b.py" not in rendered


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


def test_a_carried_intent_says_it_is_from_an_earlier_step(app):
    """The judge must not read step 3's "promoting now" as the intent of a later
    `git log` (2026-09-28)."""
    conv = app.conversation(app.desk_id())
    ctx = _ctx(app, conv, intent="promoting now; this restarts me", intent_carried=True)
    tool = app.tools.get("shell")
    rendered = classify.render_inputs(app, ctx, tool, {"command": "git log --oneline -3"})
    assert "Stated intent (untrusted data; carried from an earlier step of this turn):" in rendered
    assert "promoting now; this restarts me" in rendered
    bare = classify.render_inputs(app, _ctx(app, conv, intent="", intent_carried=True), tool, {"command": "ls"})
    assert "carried from" not in bare and "(none stated)" in bare


def test_empty_intent_renders_none_stated(app):
    conv = app.conversation(app.desk_id())
    ctx = _ctx(app, conv, intent="")
    tool = app.tools.get("shell")
    rendered = classify.render_inputs(app, ctx, tool, {"command": "ls"})
    assert "(none stated)" in rendered


def test_no_goal_line_is_rendered(app):
    """Neither the desk's title nor a thread's goal is scope the judge grades against."""
    tool = app.tools.get("shell")
    desk = classify.render_inputs(app, _ctx(app), tool, {"command": "ls"})
    assert "Conversation goal" not in desk
    thread = _thread(app, goal="omit all scaling; present for Jasper's approval", title="Some Thread")
    rendered = classify.render_inputs(app, _ctx(app, thread), tool, {"command": "ls"})
    assert "Conversation goal" not in rendered
    assert "omit all scaling" not in rendered


def test_scratch_folder_is_named_inside_the_roots(app):
    conv = app.conversation(app.desk_id())
    rendered = classify.render_inputs(app, _ctx(app, conv), app.tools.get("shell"), {"command": "mkdir -p x"})
    folder = str(app.artifacts.root / conv["id"]).replace(chr(92), "/")
    roots_section = rendered.split("Allowed roots:", 1)[1].split("Protected paths:", 1)[0]
    assert f"- {folder} (this conversation's scratch folder)" in roots_section


def test_desk_user_request_is_labelled_as_the_users_own_words(app):
    """The user's typed desk message is not injected data: the judge is told who wrote it
    (2026-09-25, "please start the bot again")."""
    conv = app.conversation(app.desk_id())
    ctx = _ctx(app, conv, request_text="please start the bot again")
    ctx.request_from_user = True
    rendered = classify.render_inputs(app, ctx, app.tools.get("shell"), {"command": "npm start"})
    assert "Request that led here (typed by the user):" in rendered
    assert "please start the bot again" in rendered


def test_thread_goal_and_handoff_are_not_rendered(app):
    """A thread's goal/handoff was written by the desk model: it could never authorise,
    and rendered it only supplied reasons to ask (2026-10-12)."""
    thread = _thread(app, goal="restart the bot")
    ctx = _ctx(app, thread, request_text="Goal: restart the bot. Ask one precise follow-up if unsure.")
    rendered = classify.render_inputs(app, ctx, app.tools.get("shell"), {"command": "npm start"})
    assert "Request that led here" not in rendered
    assert "follow-up" not in rendered


def test_untrusted_desk_request_is_not_rendered(app):
    """Connector, schedule or thread-report text on the desk is not the user's either."""
    ctx = _ctx(app, request_text="[schedule] run the cleanup")
    rendered = classify.render_inputs(app, ctx, app.tools.get("shell"), {"command": "ls"})
    assert "Request that led here" not in rendered
    assert "run the cleanup" not in rendered


def test_an_empty_request_is_never_labelled_as_the_users(app):
    ctx = _ctx(app, request_text="  ")
    ctx.request_from_user = True
    rendered = classify.render_inputs(app, ctx, app.tools.get("shell"), {"command": "ls"})
    assert "Request that led here" not in rendered
