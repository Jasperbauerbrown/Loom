"""Write-protected paths (2026-10-12): a protected path's `mode` is `all` (any touch asks)
or `writes` (only a call that writes, edits, moves or deletes asks). The default `writes`
row is `<vault>/procedures/*/scripts`: threads run a procedure's helper by name, so
editing it asks while reading and running it stay free."""

from __future__ import annotations

import dataclasses

import pytest

from loom import bootstrap, db
from loom.conversation import context as context_mod
from loom.models.fake import Scripted
from loom.models.types import ToolCall
from loom.tools import approvals, classify, dispatch, fs_guard, shell_prefilter
from loom.tools.dispatch import ApprovalNeeded
from loom.tools.registry import ToolContext, ToolResult

ALLOWLIST = ["ls", "cat", "grep", "find", "cd", "echo"]


def _scripts_glob(app) -> str:
    return fs_guard.normalise(str(app.vault_dir / "procedures" / "*" / "scripts"))


@pytest.fixture
def script(app):
    path = app.vault_dir / "procedures" / "slice" / "scripts" / "scale.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("print('bounds only')\n", encoding="utf-8")
    return path


@pytest.fixture
def stub_shell(app):
    """The real shell tool's declaration with a handler that records instead of running."""
    ran: list[str] = []

    async def handler(ctx, args):
        ran.append(args["command"])
        return ToolResult(content="[exit 0]")

    tool = dataclasses.replace(app.tools.get("shell"), handler=handler)
    app.tools.register(tool, replace=True)
    tool.ran = ran  # type: ignore[attr-defined]
    return tool


def _desk_ctx(app, call_id="c1") -> ToolContext:
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id=call_id, event_id=11, request_text="x")


def _thread_ctx(app, call_id="c1") -> ToolContext:
    thread = {**app.conversation(app.desk_id()), "kind": "thread"}
    return ToolContext(app=app, conversation=thread, call_id=call_id, event_id=11, request_text="x")


def _call(name, arguments, call_id="c1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


# -- migration and bootstrap ----------------------------------------------------------
def test_migration_0008_keeps_existing_rows_protected_for_everything(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    for number, path in db.list_migrations():
        if number > 7:
            break
        with db.transaction(conn):
            for statement in db.split_statements(path.read_text(encoding="utf-8")):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={number}")
    conn.execute("INSERT INTO protected_paths(path) VALUES ('C:/old/secrets.json')")
    assert db.migrate(conn) == [8]
    assert dict(conn.execute("SELECT * FROM protected_paths").fetchone()) == {"path": "C:/old/secrets.json", "mode": "all"}
    conn.close()


def test_a_fresh_install_seeds_the_procedure_scripts_row(app):
    rows = dict(fs_guard.protected_rows(app))
    assert rows[_scripts_glob(app)] == "writes"
    assert fs_guard.normalise(str(app.home / "secrets.json")) in fs_guard.protected_paths(app, "all")


def test_the_scripts_row_arrives_once_with_the_migration_and_a_deleted_one_stays_gone(app):
    glob = _scripts_glob(app)
    fs_guard.remove_protected(app, glob)
    bootstrap.ensure(app)  # an ordinary restart
    assert glob not in fs_guard.protected_paths(app)
    bootstrap.ensure(app, [8])  # the start that applies migration 0008 on an existing install
    assert dict(fs_guard.protected_rows(app))[glob] == "writes"


# -- the matcher ----------------------------------------------------------------------
def test_a_glob_entry_matches_per_segment(app, script):
    assert fs_guard.protection(app, str(script)) == "writes"
    assert fs_guard.protection(app, str(script.parent)) == "writes"
    assert fs_guard.protection(app, str(script.parent.parent / "SKILL.md")) is None
    assert fs_guard.protection(app, str(app.vault_dir / "procedures" / "a" / "b" / "scripts" / "x.py")) is None
    assert fs_guard.is_protected(app, str(script)) is True  # either mode counts (promotion)


def test_all_beats_writes_for_the_same_path(app, script):
    fs_guard.add_protected(app, str(script))
    assert fs_guard.protection(app, str(script)) == "all"


# -- the fs guard ---------------------------------------------------------------------
def test_writing_a_procedure_script_asks_and_reading_it_does_not(app, script):
    conv = app.conversation(app.desk_id())
    write = fs_guard.check(app, conv, app.tools.get("fs_write"), {"path": str(script), "content": "x"})
    assert isinstance(write, fs_guard.GuardAsk) and write.protected
    assert "write-protected path" in write.reason
    edit = fs_guard.check(app, conv, app.tools.get("fs_edit"), {"path": str(script), "old": "a", "new": "b"})
    assert isinstance(edit, fs_guard.GuardAsk) and edit.protected
    read = fs_guard.check(app, conv, app.tools.get("fs_read"), {"path": str(script)})
    assert isinstance(read, fs_guard.GuardOk)


def test_a_write_beside_the_scripts_is_unaffected(app, script):
    conv = app.conversation(app.desk_id())
    skill = script.parent.parent / "SKILL.md"
    assert isinstance(fs_guard.check(app, conv, app.tools.get("fs_write"), {"path": str(skill), "content": "x"}), fs_guard.GuardOk)
    references = script.parent.parent / "references" / "notes.txt"
    assert isinstance(fs_guard.check(app, conv, app.tools.get("fs_write"), {"path": str(references), "content": "x"}), fs_guard.GuardOk)


def test_an_all_row_still_asks_for_a_read(app):
    conv = app.conversation(app.desk_id())
    read = fs_guard.check(app, conv, app.tools.get("fs_read"), {"path": str(app.home / "secrets.json")})
    assert isinstance(read, fs_guard.GuardAsk) and read.reason.startswith("protected path")


# -- the shell pre-filter -------------------------------------------------------------
def test_the_pre_filter_grants_a_read_of_a_script(app, script):
    allp, writes = fs_guard.protected_paths(app, "all"), fs_guard.protected_paths(app, "writes")
    assert shell_prefilter.is_readonly(f"cat {script.as_posix()}", ALLOWLIST, allp, None, writes)
    assert shell_prefilter.is_readonly(f"cd {script.parent.as_posix()} && grep bounds scale.py", ALLOWLIST, allp, None, writes)


def test_the_pre_filter_never_grants_a_delete_of_a_script(app, script):
    allp, writes = fs_guard.protected_paths(app, "all"), fs_guard.protected_paths(app, "writes")
    assert not shell_prefilter.is_readonly(f"rm {script.as_posix()}", ALLOWLIST, allp, None, writes)
    assert not shell_prefilter.is_readonly(f"find {script.parent.as_posix()} -delete", ALLOWLIST, allp, None, writes)
    # a delete of the folder above every procedure takes the scripts with it
    procedures = (app.vault_dir / "procedures").as_posix()
    assert not shell_prefilter.is_readonly(f"find {procedures} -name '*.pyc' -delete", ALLOWLIST, allp, None, writes)
    assert shell_prefilter.is_readonly(f"find {procedures} -name '*.py'", ALLOWLIST, allp, None, writes)


async def test_a_thread_reading_a_script_is_granted_without_a_judge(app, fake, script, stub_shell):
    ctx = _thread_ctx(app)
    result = await dispatch.dispatch(app, ctx, _call("shell", {"command": f"cat {script.as_posix()}"}))
    assert result.ok and fake.calls == []
    assert "pre-filter" in ctx.meta["reason"]


# -- desk auto-approve ----------------------------------------------------------------
async def test_desk_auto_approve_runs_a_plain_read_of_a_script(app, fake, script, stub_shell):
    app.settings.set(app.db, "permissions.desk_auto_approve", True)
    ctx = _desk_ctx(app)
    result = await dispatch.dispatch(app, ctx, _call("shell", {"command": f"cat {script.as_posix()}"}))
    assert result.ok and ctx.meta["policy"] == "auto" and fake.calls == []


async def test_desk_auto_approve_sends_running_a_script_to_the_judge(app, fake, script, stub_shell):
    app.settings.set(app.db, "permissions.desk_auto_approve", True)
    fake.on("classify", lambda req: Scripted(content='{"decision":"allow","reason":"runs a bounds-only helper"}'))
    ctx = _desk_ctx(app)
    result = await dispatch.dispatch(app, ctx, _call("shell", {"command": f"python {script.as_posix()} model.stl"}))
    assert result.ok and len(fake.calls) == 1
    assert "write-protected" in ctx.meta["auto_held"]


async def test_desk_auto_approve_never_lets_a_script_edit_through(app, fake, script, stub_shell):
    app.settings.set(app.db, "permissions.desk_auto_approve", True)
    fake.on("classify", lambda req: Scripted(content='{"decision":"ask","reason":"deletes a procedure script"}'))
    outcome = await dispatch.dispatch(app, _desk_ctx(app), _call("shell", {"command": f"rm {script.as_posix()}"}))
    assert isinstance(outcome, ApprovalNeeded) and "write-protected" in outcome.reason
    outcome = await dispatch.dispatch(
        app, _desk_ctx(app, "c2"), _call("fs_write", {"path": str(script), "content": "x"}, "c2")
    )
    assert isinstance(outcome, ApprovalNeeded) and "write-protected path" in outcome.reason
    assert stub_shell.ran == []


async def test_desk_auto_approve_still_runs_an_fs_read_of_a_script(app, script):
    app.settings.set(app.db, "permissions.desk_auto_approve", True)
    result = await dispatch.dispatch(app, _desk_ctx(app), _call("fs_read", {"path": str(script)}))
    assert result.ok and "bounds only" in result.content


# -- the judge ------------------------------------------------------------------------
def test_the_judge_sees_write_protected_paths_in_their_own_list_and_the_script(app, script):
    ctx = _thread_ctx(app)
    text = classify.render_inputs(app, ctx, app.tools.get("shell"), {"command": f"python {script.as_posix()}"})
    header = "Write-protected paths (reading and running are fine; writing, editing, moving or deleting asks):"
    assert header in text
    listed = text.split(header, 1)[1].split("\n\n", 1)[0]
    assert _scripts_glob(app) in listed
    protected = text.split("Protected paths:", 1)[1].split("\n\n", 1)[0]
    assert _scripts_glob(app) not in protected
    assert "print('bounds only')" in text  # a write-protected script is shown; reading it is free


def test_the_context_block_names_write_protected_paths(app):
    block = context_mod.filesystem_block(app, app.conversation(app.desk_id()))
    assert "write-protected (changing them asks; reading and running do not): " + _scripts_glob(app) in block


# -- knowledge_write ------------------------------------------------------------------
async def test_knowledge_write_refuses_a_script_before_anyone_is_asked(app, fake):
    ctx = _thread_ctx(app)
    result = await dispatch.dispatch(
        app, ctx, _call("knowledge_write", {"path": "procedures/slice/scripts/scale.py", "op": "create", "content": "x"})
    )
    assert not isinstance(result, ApprovalNeeded) and not result.ok
    assert "fs_write" in result.content
    md = await dispatch.dispatch(
        app, ctx, _call("knowledge_write", {"path": "procedures/slice/scripts/README.md", "op": "create", "content": "x"}, "c2")
    )
    assert not isinstance(md, ApprovalNeeded) and not md.ok and "protected" in md.content
    assert not (app.vault_dir / "procedures" / "slice" / "scripts" / "README.md").exists()


# -- the API --------------------------------------------------------------------------
async def test_the_api_lists_adds_and_strengthens_with_a_mode(client, app, tmp_path):
    listed = (await client.get("/api/protected-paths")).json()
    assert {"path": _scripts_glob(app), "mode": "writes"} in listed
    added = await client.post("/api/protected-paths", json={"path": str(tmp_path / "tools"), "mode": "writes"})
    assert added.status_code == 200 and added.json()["mode"] == "writes"
    stronger = await client.put("/api/protected-paths", json={"path": str(tmp_path / "tools"), "mode": "all"})
    assert stronger.status_code == 200 and stronger.json()["mode"] == "all"
    assert fs_guard.protection(app, str(tmp_path / "tools" / "x.py")) == "all"
    bad = await client.post("/api/protected-paths", json={"path": str(tmp_path / "y"), "mode": "reads"})
    assert bad.status_code == 400
    missing = await client.put("/api/protected-paths", json={"path": str(tmp_path / "nope"), "mode": "all"})
    assert missing.status_code == 404


async def test_weakening_a_protection_is_an_approval(client, app, tmp_path, monkeypatch):
    monkeypatch.setattr("loom.conversation.wake", lambda app, cid: pytest.fail("no turn is waiting"))
    path = fs_guard.add_protected(app, str(tmp_path / "keys"))
    response = await client.put("/api/protected-paths", json={"path": path, "mode": "writes"})
    assert response.status_code == 202
    approval = response.json()["approval"]
    assert approval["tool"] == approvals.INTERNAL_TOOL and approval["args"] == {"path": path, "mode": "writes"}
    assert fs_guard.protected_mode(app, path) == "all"  # nothing weakened yet
    decided = await client.post(f"/api/approvals/{approval['id']}", json={"decision": "allow"})
    assert decided.status_code == 200
    assert fs_guard.protected_mode(app, path) == "writes"  # weakened, not removed
