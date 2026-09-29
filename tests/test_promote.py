"""Self-modification, backend half (03 §15.2 steps 1–4, §15.3, §17).

No test promotes this repository and none starts a supervisor: `plan` takes the repo it
is pointed at, and the supervisor is a loopback HTTP client that is either absent (the
clear error) or stubbed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from loom import items, promote
from loom.conversation import events
from loom.tools import fs_guard
from loom.tools.registry import ToolContext


# ---------------------------------------------------------------------------------
# a temp repo with two commits, the second touching a protected path
# ---------------------------------------------------------------------------------
def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stdout}{proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path, app, monkeypatch):
    """A two-commit repo standing in for the assistant's own checkout, with
    `supervisor/` registered as a protected path exactly as the bootstrap does."""
    path = tmp_path / "self-repo"
    path.mkdir()
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "t@example.invalid", cwd=path)
    git("config", "user.name", "t", cwd=path)
    (path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (path / "notes.md").write_text("one\n", encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-qm", "one", cwd=path)
    first = git("rev-parse", "HEAD", cwd=path)
    (path / "supervisor").mkdir()
    (path / "supervisor" / "__main__.py").write_text("# touched\n", encoding="utf-8")
    (path / "notes.md").write_text("two\n", encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-qm", "two", cwd=path)
    second = git("rev-parse", "HEAD", cwd=path)
    (path / "notes.md").write_text("three\n", encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-qm", "three", cwd=path)
    third = git("rev-parse", "HEAD", cwd=path)  # touches nothing protected

    monkeypatch.setattr(promote, "repo_root", lambda _app: path)
    fs_guard.add_protected(app, str(path / "supervisor"))
    return {"path": path, "first": first, "second": second, "third": third}


def live_at(repo, sha: str) -> None:
    """Put the running checkout on `sha`, so a plan diffs against it."""
    git("checkout", "-q", "--detach", sha, cwd=repo["path"])


def ctx_for(app) -> ToolContext:
    cid = app.desk_id()
    return ToolContext(app=app, conversation=app.conversation(cid), call_id="c1")


# ---------------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------------
def test_plan_lists_the_changed_files_and_the_protected_ones(app, repo):
    live_at(repo, repo["first"])
    plan = promote.plan(app, repo["second"])
    assert plan.ok and plan.commit == repo["second"] and plan.short == repo["second"][:7]
    assert plan.current_commit == repo["first"]
    assert sorted(plan.changed_files) == ["notes.md", "supervisor/__main__.py"]
    assert plan.protected_files == ["supervisor/__main__.py"]
    assert plan.tests_required is True
    assert "PROTECTED" in plan.summary()


def test_plan_of_the_live_commit_changes_nothing(app, repo):
    live_at(repo, repo["second"])
    plan = promote.plan(app, repo["second"])
    assert plan.ok and plan.changed_files == [] and plan.protected_files == []


def test_plan_resolves_a_branch_name(app, repo):
    live_at(repo, repo["first"])
    git("checkout", "-q", "-b", "later", repo["second"], cwd=repo["path"])
    git("checkout", "-q", "--detach", repo["first"], cwd=repo["path"])
    plan = promote.plan(app, "later")
    assert plan.ok and plan.commit == repo["second"]


def test_plan_reports_an_unresolvable_ref(app, repo):
    plan = promote.plan(app, "no-such-thing")
    assert not plan.ok and plan.code == "bad_ref" and "does not resolve" in plan.error


def test_plan_sees_an_unclean_worktree_holding_the_ref(app, repo):
    live_at(repo, repo["third"])
    (repo["path"] / "notes.md").write_text("dirty\n", encoding="utf-8")
    plan = promote.plan(app, repo["third"])
    assert plan.ok and plan.clean is False and "notes.md" in plan.dirty_files
    assert plan.worktree


def test_plan_without_a_checkout_says_so(app, monkeypatch):
    monkeypatch.setattr(promote, "repo_root", lambda _app: None)
    plan = promote.plan(app, "HEAD")
    assert not plan.ok and plan.code == "no_repo"


# ---------------------------------------------------------------------------------
# the promote tool (03 §15.2 steps 1–4)
# ---------------------------------------------------------------------------------
@pytest.fixture
def no_tests(monkeypatch):
    """The suite is the slow part and has its own failure test; pass it by default."""
    async def _ok(worktree, timeout_s=promote.TEST_TIMEOUT_S):
        return True, "2 passed"

    monkeypatch.setattr(promote, "run_tests", _ok)


@pytest.fixture
def supervisor_ok(app, monkeypatch):
    seen: list[tuple[str, str]] = []

    async def _promote(self, ref, conversation_id=""):
        seen.append((ref, conversation_id))
        return {"ok": True, "queued": "promote"}

    monkeypatch.setattr(promote.SupervisorClient, "promote", _promote)
    return seen


def tool(app, name: str):
    found = app.tools.get(name)
    assert found is not None, f"{name} is not registered"
    return found


def test_both_tools_are_registered_with_the_right_shape(app):
    assert tool(app, "promote_plan").read_only is True
    assert tool(app, "promote").read_only is False
    assert tool(app, "promote").destructive is True
    row = app.db.execute("SELECT mode FROM tool_policies WHERE tool='promote'").fetchone()
    assert row["mode"] == "ask"  # the bootstrap row: a human is in the loop for the call


async def test_promote_refuses_a_protected_diff_and_names_the_files(app, repo, no_tests, supervisor_ok):
    live_at(repo, repo["first"])
    result = await tool(app, "promote").handler(ctx_for(app), {"ref": repo["second"]})
    assert result.ok is False
    assert "supervisor/__main__.py" in result.content
    assert "acknowledge_protected" in result.content
    assert result.meta["code"] == "protected"
    assert supervisor_ok == []  # nothing was handed over


async def test_promote_proceeds_once_the_protected_paths_are_acknowledged(app, repo, no_tests, supervisor_ok):
    live_at(repo, repo["first"])
    result = await tool(app, "promote").handler(
        ctx_for(app), {"ref": repo["second"], "acknowledge_protected": True}
    )
    assert result.ok and result.meta["ends_turn"] is True
    assert supervisor_ok == [(repo["second"], app.desk_id())]
    last = events.last(app, app.desk_id(), 1, kinds=["system"])
    assert last and "promotion of" in last[0]["content"] and "this turn ends" in last[0]["content"]


async def test_promote_of_an_unprotected_diff_needs_no_acknowledgement(app, repo, no_tests, supervisor_ok):
    """The third commit touches only `notes.md`, so nothing extra is asked."""
    live_at(repo, repo["second"])
    result = await tool(app, "promote").handler(ctx_for(app), {"ref": repo["third"]})
    assert result.ok and supervisor_ok == [(repo["third"], app.desk_id())]


async def test_promote_stops_on_a_failing_test_suite_and_reports_the_output(app, repo, supervisor_ok, monkeypatch):
    async def _fail(worktree, timeout_s=promote.TEST_TIMEOUT_S):
        return False, "E   assert 1 == 2\n1 failed"

    monkeypatch.setattr(promote, "run_tests", _fail)
    live_at(repo, repo["second"])
    result = await tool(app, "promote").handler(ctx_for(app), {"ref": repo["third"]})
    assert result.ok is False and result.meta["code"] == "tests_failed"
    assert "assert 1 == 2" in result.content
    assert supervisor_ok == []


async def test_promote_stops_on_an_unclean_worktree(app, repo, no_tests, supervisor_ok):
    live_at(repo, repo["third"])
    (repo["path"] / "notes.md").write_text("dirty\n", encoding="utf-8")
    result = await tool(app, "promote").handler(ctx_for(app), {"ref": repo["third"]})
    assert result.ok is False and result.meta["code"] == "dirty"
    assert "notes.md" in result.content and supervisor_ok == []


async def test_promote_without_a_supervisor_says_how_to_get_one(app, repo, no_tests):
    live_at(repo, repo["second"])
    assert not (app.home / "supervisor.token").exists()
    result = await tool(app, "promote").handler(ctx_for(app), {"ref": repo["third"]})
    assert result.ok is False and result.meta["code"] == "no_supervisor"
    assert result.content.endswith(promote.NOT_RUNNING) or promote.NOT_RUNNING in result.content


async def test_promote_plan_tool_is_read_only_and_summarises(app, repo):
    live_at(repo, repo["first"])
    result = await tool(app, "promote_plan").handler(ctx_for(app), {"ref": repo["second"]})
    assert result.ok and result.meta["protected"] == ["supervisor/__main__.py"]
    assert repo["second"][:7] in result.content


# ---------------------------------------------------------------------------------
# the supervisor client
# ---------------------------------------------------------------------------------
async def test_every_supervisor_call_answers_clearly_when_it_is_not_running(app):
    client = promote.SupervisorClient(app)
    assert client.running is False
    for call in (client.status(), client.promote("x"), client.rollback(), client.restart()):
        answer = await call
        assert answer == {"ok": False, "error": promote.NOT_RUNNING}


async def test_an_unreachable_supervisor_is_the_same_failure(app):
    (app.home / "supervisor.token").write_text("deadbeef", encoding="utf-8")
    app.settings.set(app.db, "general.port", 8599)  # nothing listens on 8600
    answer = await promote.SupervisorClient(app).status()
    assert answer["ok"] is False and answer["error"] == promote.NOT_RUNNING


def test_the_client_talks_to_port_plus_one(app):
    app.settings.set(app.db, "general.port", 8500)
    assert promote.SupervisorClient(app).port == 8501


# ---------------------------------------------------------------------------------
# the progress log (step 10, §17)
# ---------------------------------------------------------------------------------
def write_log(app, *entries: dict) -> None:
    with (app.home / promote.PROGRESS_LOG).open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def entry(stage: str, ok: bool = True, message: str = "m", conversation_id: str = "") -> dict:
    return {"ts": "2026-09-20T10:00:00Z", "stage": stage, "ok": ok, "message": message,
            "ref": "abc1234", "conversation_id": conversation_id}


def test_read_progress_republishes_each_line_once(app):
    seen: list[dict] = []
    app.bus.listen(lambda e: seen.append(e) if e["type"] == "promote.progress" else None)
    write_log(app, entry("snapshot"), entry("release"))
    assert len(promote.read_progress(app)) == 2
    assert [e["data"]["stage"] for e in seen] == ["snapshot", "release"]
    assert promote.read_progress(app) == []  # the offset is durable
    write_log(app, entry("probe"))
    assert [e["stage"] for e in promote.read_progress(app)] == ["probe"]


def test_a_finished_promotion_posts_an_item_and_a_system_event(app):
    cid = app.desk_id()
    write_log(app, entry("done", True, "promoted 20260920-abc1234", cid))
    promote.read_progress(app)
    posted = items.list_items(app)
    assert posted and posted[0]["source"] == "promote"
    assert "succeeded" in posted[0]["title"] and posted[0]["conversation_id"] == cid
    last = events.last(app, cid, 1, kinds=["system"])
    assert last and "promoted 20260920-abc1234" in last[0]["content"]


def test_a_failed_promotion_notifies(app):
    seen: list[dict] = []
    app.bus.listen(lambda e: seen.append(e) if e["type"] == "notification" else None)
    write_log(app, entry("failed", False, "watchdog", app.desk_id()))
    promote.read_progress(app)
    assert seen and "failed" in seen[0]["data"]["title"]


def test_a_truncated_log_is_read_from_the_start_again(app):
    write_log(app, entry("snapshot"))
    promote.read_progress(app)
    (app.home / promote.PROGRESS_LOG).write_text("", encoding="utf-8")
    write_log(app, entry("release"))
    assert [e["stage"] for e in promote.read_progress(app)] == ["release"]


async def test_start_reports_what_happened_while_the_backend_was_down(app):
    """A promotion's last stages are written while this process does not exist, so the
    first read happens at startup."""
    cid = app.desk_id()
    write_log(app, entry("done", True, "promoted after the restart", cid))
    await promote.start(app)
    try:
        assert "promote.tail" in app.tasks
        last = events.last(app, cid, 1, kinds=["system"])
        assert last and "promoted after the restart" in last[0]["content"]
    finally:
        await promote.stop(app)


def test_read_log_returns_the_whole_log_for_the_system_screen(app):
    write_log(app, entry("snapshot"), entry("done"))
    assert [e["stage"] for e in promote.read_log(app)] == ["snapshot", "done"]
    assert promote.read_log(app, limit=1)[0]["stage"] == "done"


def test_garbage_in_the_log_is_skipped_not_fatal(app):
    (app.home / promote.PROGRESS_LOG).write_text("not json\n", encoding="utf-8")
    write_log(app, entry("done"))
    assert [e["stage"] for e in promote.read_progress(app)] == ["done"]


# ---------------------------------------------------------------------------------
# the procedure note (charter §5.9: the workflow lives in the vault)
# ---------------------------------------------------------------------------------
def test_install_writes_the_self_modification_procedure(app):
    note = app.vault.read(promote.PROCEDURE_PATH)
    assert "worktree" in note.body and "promote_plan" in note.body
    assert note.frontmatter.get("approved")  # written by loom itself (03 §8.4)
    # The thread is spawned isolated, so its checkout is a root from the first turn; a
    # checkout made by hand sat outside every root and asked on every file call
    # (2026-09-28). The one `git worktree add` left is the warning not to.
    assert 'isolation: "worktree"' in note.body
    assert "releases/dev-" not in note.body and "APP_HOME>/scratch" not in note.body
    assert note.body.count("git worktree add") == 1 and "Never make the worktree by hand" in note.body


# ---------------------------------------------------------------------------------
# REST (03 §12.1)
# ---------------------------------------------------------------------------------
async def test_plan_endpoint_returns_the_plan(client, repo, app):
    live_at(repo, repo["first"])
    response = await client.get("/api/system/promote/plan", params={"ref": repo["second"]})
    assert response.status_code == 200
    body = response.json()
    assert body["protected_files"] == ["supervisor/__main__.py"] and body["commit"] == repo["second"]


async def test_plan_endpoint_rejects_an_unknown_ref(client, repo):
    response = await client.get("/api/system/promote/plan", params={"ref": "nope"})
    assert response.status_code == 400


async def test_promote_endpoint_is_409_until_protected_paths_are_acknowledged(client, repo, no_tests):
    live_at(repo, repo["first"])
    response = await client.post("/api/system/promote", json={"ref": repo["second"]})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["protected_files"] == ["supervisor/__main__.py"]


async def test_promote_endpoint_is_503_without_a_supervisor(client, repo, no_tests):
    live_at(repo, repo["second"])
    response = await client.post("/api/system/promote", json={"ref": repo["third"]})
    assert response.status_code == 503
    assert promote.NOT_RUNNING in response.json()["detail"]


async def test_promote_endpoint_requires_a_ref(client):
    assert (await client.post("/api/system/promote", json={})).status_code == 400


async def test_rollback_and_restart_are_503_without_a_supervisor(client):
    assert (await client.post("/api/system/rollback", json={})).status_code == 503
    assert (await client.post("/api/system/restart")).status_code == 503


async def test_supervisor_status_endpoint_reports_not_running(client):
    body = (await client.get("/api/system/supervisor")).json()
    assert body["running"] is False and promote.NOT_RUNNING in body["error"]


async def test_log_and_snapshot_endpoints(client, app):
    write_log(app, entry("done"))
    log = (await client.get("/api/system/promote/log")).json()
    assert [e["stage"] for e in log] == ["done"]

    assert (await client.get("/api/system/snapshots")).json() == []
    folder = app.snapshots_dir / "20260920-120000"
    folder.mkdir(parents=True)
    (folder / "state.db").write_bytes(b"x" * 10)
    rows = (await client.get("/api/system/snapshots")).json()
    assert rows[0]["id"] == "20260920-120000" and rows[0]["bytes"] == 10 and rows[0]["has_state"]
