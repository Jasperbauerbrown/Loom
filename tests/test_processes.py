"""Background processes (03 §9.6): start through the shell tool, read and stop through
the process tool and REST, the exit message to the owner, reconciliation after a
restart, pruning. Children are `sys.executable -c …` so the suite runs on every OS."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

from loom import db, processes
from loom.conversation import context, inbox
from loom.tools import dispatch, fs_guard
from loom.tools.registry import ToolContext

PY = sys.executable.replace("\\", "/")


def _cmd(code: str) -> str:
    """A one-line python child through the configured shell."""
    return f'"{PY}" -c "{code}"'


async def wait_for(pred, timeout: float = 20.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if pred():
                return
        except Exception:
            pass
        await asyncio.sleep(0.1)
    raise AssertionError("condition not met in time")


def _inbox(app, process_id: str):  # type: ignore[no-untyped-def]
    return app.db.execute("SELECT * FROM inbox WHERE source=? ORDER BY id DESC", (f"process:{process_id}",)).fetchall()


@pytest.fixture
def project(app, tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    fs_guard.add_root(app, str(root), added_by="test")
    return root


@pytest.fixture
def ctx(app, project):
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1", event_id=1)


@pytest.fixture(autouse=True)
async def _no_orphans(app):
    """Whatever a test started is killed at its end, and the turns the exit messages
    woke are cancelled before the app fixture closes the database."""
    yield
    pids = [proc.pid for proc in app.processes._procs.values()]
    try:
        pids += [row.get("pid") for row in app.processes.list(status="running")]
    except Exception:  # the client fixture's lifespan may already have closed the DB
        pass
    for pid in pids:
        processes.kill_tree(pid)
    for task in list(app.tasks.values()):
        if not task.done():
            task.cancel()
    await asyncio.sleep(0)


# -- shell(background=true) + the process tool ------------------------------------------

async def test_shell_background_starts_a_supervised_process(app, ctx, project):
    shell = app.tools.get("shell")
    res = await dispatch.execute(
        app, ctx, shell,
        {"command": _cmd("import time; print('hello from child', flush=True); time.sleep(60)"), "cwd": str(project), "background": True, "title": "sleeper"},
    )
    assert res.ok, res.content
    process_id = res.meta["process_id"]
    assert "started process" in res.content and 'process(action="logs"' in res.content

    row = app.processes.get(process_id)
    assert row["status"] == "running" and row["pid"] and row["title"] == "sleeper"
    assert row["command"].startswith(f'"{PY}"') and Path(row["cwd"]) == project
    assert Path(row["log_path"]).parent == Path(app.artifacts_dir) / ctx.conversation_id
    assert processes.pid_alive(row["pid"])
    await wait_for(lambda: "hello from child" in Path(row["log_path"]).read_text(encoding="utf-8"))

    # The now board names it, with its owner.
    board = context.now_board(app)
    assert f'- process "sleeper" ({process_id}): running' in board and "pid" in board

    tool = app.tools.get("process")
    assert tool.paged and tool.is_read_only({"action": "logs"}) and not tool.is_read_only({"action": "stop"})
    logs = await dispatch.execute(app, ctx, tool, {"action": "logs", "id": process_id})
    assert logs.ok and "hello from child" in logs.content and "artifact_id" not in logs.meta
    listed = await dispatch.execute(app, ctx, tool, {"action": "list"})
    assert listed.ok and process_id in listed.content and "| running |" in listed.content

    stopped = await dispatch.execute(app, ctx, tool, {"action": "stop", "id": process_id})
    assert stopped.ok and f"stopped process {process_id}" in stopped.content
    row = app.processes.get(process_id)
    assert row["status"] == "stopped" and row["ended_at"] and row["error"] == f"stopped by conversation:{ctx.conversation_id}"
    await wait_for(lambda: not processes.pid_alive(row["pid"]))
    assert _inbox(app, process_id) == []  # the stopper knows; no message to itself
    again = await dispatch.execute(app, ctx, tool, {"action": "stop", "id": process_id})
    assert again.ok and "is not running: stopped" in again.content
    assert "no process" in (await dispatch.execute(app, ctx, tool, {"action": "logs", "id": "nope"})).content


async def test_logs_page_like_recall(app, ctx, project, fake):
    fake.reply("ok")
    row = await app.processes.start(ctx.conversation_id, _cmd("print('0123456789' * 60, end='', flush=True)"), str(project), title="pager")
    await wait_for(lambda: app.processes.get(row["id"])["status"] == "exited")
    app.settings.set(app.db, "conversation.tool_output_cap", "500")  # the smallest the setting allows
    tool = app.tools.get("process")
    first = await dispatch.execute(app, ctx, tool, {"action": "logs", "id": row["id"], "offset": 0})
    assert first.content.startswith("0123456789" * 50) and f'[chars 0–500 of 600; process(action="logs", id="{row["id"]}", offset=500) for more]' in first.content
    assert "artifact_id" not in first.meta  # paged: never spilled, even at exactly the cap
    tail = await dispatch.execute(app, ctx, tool, {"action": "logs", "id": row["id"]})
    assert "[showing the last 500 chars of 600; offset=0 for the start]" in tail.content
    page = app.processes.read_log(row["id"], offset=None, limit=5000)  # clamped to the cap
    assert page["offset"] == 100 and page["end"] == 600 and page["total"] == 600


async def test_a_finished_process_posts_to_its_conversation(app, ctx, project, fake):
    fake.reply("noted")  # the message wakes the desk; give the fake model one line
    row = await app.processes.start(ctx.conversation_id, _cmd("print('bye', flush=True)"), str(project), title="short")
    assert row["status"] == "running"
    await wait_for(lambda: app.processes.get(row["id"])["status"] == "exited")
    final = app.processes.get(row["id"])
    assert final["exit_code"] == 0 and final["ended_at"]
    rows = _inbox(app, row["id"])
    assert len(rows) == 1
    content = rows[0]["content"]
    assert content.startswith("exited with code 0 after") and "bye" in content
    meta = db.loads(rows[0]["meta"], {})
    assert meta["event"] == "exited" and meta["exit_code"] == 0 and meta["title"] == "short"
    rendered = inbox.render(app, {"source": f"process:{row['id']}", "content": content, "meta": meta})
    assert rendered.startswith('[process "short" exited]\nexited with code 0')


async def test_a_spawn_failure_is_a_failed_row_not_an_exception(app, ctx, project, monkeypatch):
    from loom.tools.native import shell as shell_tool

    monkeypatch.setattr(shell_tool, "resolve_shell", lambda app: [str(project / "no-such-shell.exe"), "-c"])
    res = await dispatch.execute(app, ctx, app.tools.get("shell"), {"command": "echo x", "cwd": str(project), "background": True})
    assert not res.ok and "cannot start" in res.content
    row = app.processes.get(res.meta["process_id"])
    assert row["status"] == "failed" and row["ended_at"] and "cannot start" in row["error"]


# -- REST ---------------------------------------------------------------------------------

async def test_rest_lists_reads_and_stops(client, project, fake):
    app = client.loom
    fake.reply("ok")
    row = await app.processes.start(app.desk_id(), _cmd("import time; print('rest child', flush=True); time.sleep(60)"), str(project), title="rest")
    await wait_for(lambda: "rest child" in Path(row["log_path"]).read_text(encoding="utf-8"))

    listed = await client.get("/api/processes")
    assert listed.status_code == 200 and any(p["id"] == row["id"] and "log_bytes" in p for p in listed.json()["processes"])
    only_running = await client.get("/api/processes", params={"status": "running", "conversation_id": app.desk_id()})
    assert [p["id"] for p in only_running.json()["processes"]] == [row["id"]]
    assert (await client.get(f"/api/processes/{row['id']}")).json()["status"] == "running"
    logs = (await client.get(f"/api/processes/{row['id']}/logs")).json()
    assert "rest child" in logs["text"] and logs["total"] == logs["end"]
    assert (await client.get(f"/api/processes/{row['id']}/logs", params={"offset": 0, "limit": 4})).json()["text"] == "rest"

    stopped = await client.post(f"/api/processes/{row['id']}/stop")
    assert stopped.status_code == 200 and stopped.json()["status"] == "stopped"
    rows = _inbox(app, row["id"])  # a user stop is reported to the owner
    assert len(rows) == 1 and rows[0]["content"] == "stopped by the user" and db.loads(rows[0]["meta"], {})["event"] == "stopped"
    assert (await client.get("/api/processes/nope")).status_code == 404
    assert (await client.post("/api/processes/nope/stop")).status_code == 404
    assert (await client.get("/api/processes/nope/logs")).status_code == 404


# -- reconcile + prune ----------------------------------------------------------------------

def _insert(app, process_id: str, status: str, pid: int | None, cwd: str, conversation_id: str | None = None, started_at: str | None = None):  # type: ignore[no-untyped-def]
    app.db.execute(
        "INSERT INTO processes(id, conversation_id, title, command, cwd, pid, status, log_path, started_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (process_id, conversation_id or app.desk_id(), process_id, "x", cwd, pid, status, f"{cwd}/{process_id}.log", started_at or db.now_iso()),
    )


async def test_reconcile_sorts_out_what_a_restart_left(app, project, fake):
    fake.reply("ok")
    _insert(app, "starting1", "starting", None, str(project))
    _insert(app, "dead1", "running", 999_999_999, str(project))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    _insert(app, "live1", "running", child.pid, str(project))
    app.processes.poll_interval = 0.1
    try:
        await app.processes.reconcile()
        failed = app.processes.get("starting1")
        assert failed["status"] == "failed" and "restarted during spawn" in failed["error"] and failed["ended_at"]
        lost = app.processes.get("dead1")
        assert lost["status"] == "lost" and lost["ended_at"] and _inbox(app, "dead1") == []
        assert app.processes.get("live1")["status"] == "running"
        assert "process:live1" in app.tasks
        child.kill()
        child.wait()
        await wait_for(lambda: app.processes.get("live1")["status"] == "exited")
        row = app.processes.get("live1")
        assert row["exit_code"] is None and "backend restarted" in row["error"]
        rows = _inbox(app, "live1")
        assert len(rows) == 1 and rows[0]["content"].startswith("exited with code unknown") and "(no output)" in rows[0]["content"]
    finally:
        if child.poll() is None:
            child.kill()


async def test_prune_archived_drops_old_rows_of_archived_conversations(app, project):
    from loom.conversation import create_thread

    thread = create_thread(app, goal="old work", parent_id=app.desk_id())
    app.db.execute("UPDATE conversations SET status='archived', archived_at='2020-01-01T00:00:00Z' WHERE id=?", (thread["id"],))
    _insert(app, "old-exited", "exited", None, str(project), conversation_id=thread["id"], started_at="2020-01-01T00:00:00Z")
    _insert(app, "old-running", "running", 999_999_999, str(project), conversation_id=thread["id"], started_at="2020-01-01T00:00:00Z")
    _insert(app, "fresh", "exited", None, str(project))
    assert app.processes.prune_archived() == 1
    assert app.processes.get("old-exited") is None
    assert app.processes.get("old-running") is not None and app.processes.get("fresh") is not None
