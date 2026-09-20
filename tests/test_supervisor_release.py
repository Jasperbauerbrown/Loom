"""The supervisor's half of self-modification (03 §15.2 steps 5–10).

Nothing here promotes this repository and nothing starts a backend: the seams take a
home and a git repo, so a temp repo with two commits exercises them honestly. The
process mechanics are tested through their bookkeeping — which pointer was written
when, and what a failure puts back.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from supervisor.__main__ import (
    Supervisor,
    create_release,
    latest_snapshot,
    log_progress,
    prune_snapshots,
    restore_snapshot,
    snapshot,
    user_version,
)


# ---------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------
def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stdout}{proc.stderr}"
    return proc.stdout.strip()


def make_repo(path: Path) -> list[str]:
    """A repo with two commits; the second one touches `supervisor/`."""
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "t@example.invalid", cwd=path)
    git("config", "user.name", "t", cwd=path)
    (path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (path / "src").mkdir(exist_ok=True)
    (path / "src" / "thing.py").write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-qm", "one", cwd=path)
    first = git("rev-parse", "HEAD", cwd=path)
    (path / "supervisor").mkdir(exist_ok=True)
    (path / "supervisor" / "__main__.py").write_text("# touched\n", encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-qm", "two", cwd=path)
    return [first, git("rev-parse", "HEAD", cwd=path)]


def make_state(home: Path, version: int = 3) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()
    return home / "state.db"


def set_setting(home: Path, key: str, value) -> None:
    conn = sqlite3.connect(home / "state.db")
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?, 'now')", (key, json.dumps(value)))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------------
# step 5: snapshot
# ---------------------------------------------------------------------------------
def test_snapshot_copies_the_database_and_is_readable(tmp_path):
    home = tmp_path / "home"
    make_state(home, version=7)
    folder = snapshot(home)
    copy = folder / "state.db"
    assert copy.exists() and folder.parent == home / "snapshots"
    conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    conn.close()
    assert user_version(copy) == 7


def test_snapshot_tags_the_vault_when_there_is_one(tmp_path):
    home = tmp_path / "home"
    make_state(home)
    vault = home / "vault"
    make_repo(vault)
    folder = snapshot(home)
    tags = git("tag", "--list", cwd=vault).split()
    assert [t for t in tags if t == f"pre-{folder.name}"]


def test_snapshots_are_pruned_to_the_retention_setting(tmp_path):
    home = tmp_path / "home"
    make_state(home)
    set_setting(home, "system.snapshot_retention", 3)
    for i in range(6):
        folder = home / "snapshots" / f"2026090{i}-000000"
        folder.mkdir(parents=True)
        (folder / "state.db").write_bytes(b"")
    snapshot(home)  # a seventh, then pruning
    kept = sorted(p.name for p in (home / "snapshots").iterdir())
    assert len(kept) == 3
    assert kept[0] == "20260904-000000"  # the oldest go first
    assert latest_snapshot(home) is not None


def test_prune_never_removes_everything(tmp_path):
    home = tmp_path / "home"
    (home / "snapshots" / "a").mkdir(parents=True)
    prune_snapshots(home, 0)
    assert [p.name for p in (home / "snapshots").iterdir()] == ["a"]


def test_restore_snapshot_puts_the_database_back(tmp_path):
    home = tmp_path / "home"
    make_state(home, version=2)
    folder = snapshot(home)
    make_state(home, version=9)  # the new release migrated
    assert user_version(home / "state.db") == 9
    assert restore_snapshot(home, folder) is True
    assert user_version(home / "state.db") == 2


# ---------------------------------------------------------------------------------
# step 6: create_release
# ---------------------------------------------------------------------------------
def test_create_release_makes_a_worktree_at_the_commit(tmp_path):
    origin = tmp_path / "repo"
    shas = make_repo(origin)
    home = tmp_path / "home"
    make_state(home)
    release = create_release(home, shas[0], origin=origin, deps=False, build_ui=False)
    worktree = home / "releases" / release
    assert release.endswith(shas[0][:7])
    assert worktree.is_dir()
    assert git("rev-parse", "HEAD", cwd=worktree) == shas[0]
    assert not (worktree / "supervisor").exists()  # the first commit, not the second
    assert (home / "releases" / "src.git" / "HEAD").exists()  # the bare clone is reused


def test_create_release_refuses_a_ref_that_does_not_resolve(tmp_path):
    origin = tmp_path / "repo"
    make_repo(origin)
    home = tmp_path / "home"
    with pytest.raises(Exception) as exc:
        create_release(home, "no-such-ref", origin=origin, deps=False, build_ui=False)
    assert "does not resolve" in str(exc.value)


def test_create_release_finds_commits_pushed_after_the_clone(tmp_path):
    origin = tmp_path / "repo"
    make_repo(origin)
    home = tmp_path / "home"
    create_release(home, "main", origin=origin, deps=False, build_ui=False)
    (origin / "src" / "thing.py").write_text("VALUE = 2\n", encoding="utf-8")
    git("add", "-A", cwd=origin)
    git("commit", "-qm", "three", cwd=origin)
    third = git("rev-parse", "HEAD", cwd=origin)
    release = create_release(home, third, origin=origin, deps=False, build_ui=False)
    assert git("rev-parse", "HEAD", cwd=home / "releases" / release) == third


# ---------------------------------------------------------------------------------
# the progress log
# ---------------------------------------------------------------------------------
def test_log_progress_appends_one_json_line_per_stage(tmp_path):
    home = tmp_path / "home"
    log_progress(home, "snapshot", True, "snapshotting", "abc", "conv1")
    log_progress(home, "done", True, "promoted", "abc", "conv1")
    lines = [json.loads(l) for l in (home / "promote.log").read_text(encoding="utf-8").splitlines()]
    assert [l["stage"] for l in lines] == ["snapshot", "done"]
    assert set(lines[0]) == {"ts", "stage", "ok", "message", "ref", "conversation_id"}
    assert lines[0]["ref"] == "abc" and lines[0]["conversation_id"] == "conv1"


# ---------------------------------------------------------------------------------
# steps 8–9: the promote flow's bookkeeping (no processes; spawn/probe are stubbed)
# ---------------------------------------------------------------------------------
class FakeSupervisor(Supervisor):
    """Everything real except the child process and the health it would report."""

    def __init__(self, home, port=8500, healthy_after_swap=True):
        super().__init__(home, port)
        self.healthy_after_swap = healthy_after_swap
        self.spawns: list[str | None] = []
        self.kills = 0

    def spawn(self) -> None:
        self.spawns.append(self.current())

    def kill_child(self, grace: float = 10.0) -> None:
        self.kills += 1

    def watchdog(self, seconds: int) -> bool:
        return self.healthy_after_swap


@pytest.fixture
def flow(tmp_path, monkeypatch):
    """A supervisor whose release building and probing are replaced by recorders."""
    home = tmp_path / "home"
    make_state(home, version=4)
    calls: dict[str, object] = {"probe_ok": True, "release": "R-NEW", "probed": []}

    import supervisor.__main__ as sup

    monkeypatch.setattr(sup, "create_release", lambda home_, ref, **kw: calls["release"])
    monkeypatch.setattr(sup, "probe", lambda home_, release, port, **kw: (
        calls["probed"].append(release) or calls["probe_ok"]
    ))
    return home, calls


def stages(home: Path) -> list[tuple[str, bool]]:
    return [
        (json.loads(line)["stage"], json.loads(line)["ok"])
        for line in (home / "promote.log").read_text(encoding="utf-8").splitlines()
    ]


def test_promote_writes_previous_before_current_and_reports_every_stage(flow):
    home, calls = flow
    supervisor = FakeSupervisor(home)
    supervisor.set_current("R-OLD")
    supervisor.do_promote("deadbeef", "conv1")

    assert supervisor.current() == "R-NEW" and supervisor.previous() == "R-OLD"
    assert calls["probed"] == ["R-NEW"]
    assert supervisor.spawns == ["R-NEW"] and supervisor.kills == 1
    assert [s for s, _ in stages(home)] == ["snapshot", "release", "probe", "swap", "watchdog", "done"]
    assert stages(home)[-1] == ("done", True)
    entry = json.loads((home / "promote.log").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["conversation_id"] == "conv1" and entry["ref"] == "deadbeef"
    assert (home / "snapshots").is_dir()


def test_a_failed_probe_changes_no_pointer(flow):
    home, calls = flow
    calls["probe_ok"] = False
    supervisor = FakeSupervisor(home)
    supervisor.set_current("R-OLD")
    supervisor.do_promote("deadbeef")

    assert supervisor.current() == "R-OLD" and supervisor.previous() is None
    assert supervisor.spawns == [] and supervisor.kills == 0
    assert stages(home)[-1][0] == "failed"
    assert "probe" in [s for s, _ in stages(home)]


def test_a_failed_watchdog_restores_the_pointers_and_restarts_the_old_release(flow):
    home, _ = flow
    supervisor = FakeSupervisor(home, healthy_after_swap=False)
    supervisor.set_current("R-OLD")
    supervisor.do_promote("deadbeef")

    assert supervisor.current() == "R-OLD"
    assert supervisor.previous() is None  # the pointer it had before the swap
    assert supervisor.spawns == ["R-NEW", "R-OLD"]  # the new one, then back to the old
    assert stages(home)[-1] == ("failed", False)
    assert "rolled back" not in supervisor.last_error  # last_error names the cause
    assert "health did not hold" in supervisor.last_error


def test_a_failed_promotion_restores_the_database_when_the_release_migrated(flow, monkeypatch):
    home, _ = flow
    supervisor = FakeSupervisor(home, healthy_after_swap=False)

    real_spawn = supervisor.spawn

    def migrating_spawn() -> None:
        # the new release starts, migrates, and only then fails its watchdog
        if supervisor.current() == "R-NEW":
            make_state(home, version=9)
        real_spawn()

    monkeypatch.setattr(supervisor, "spawn", migrating_spawn)
    supervisor.do_promote("deadbeef")

    assert user_version(home / "state.db") == 4  # the snapshot came back
    assert "restored the snapshot" in (home / "promote.log").read_text(encoding="utf-8")


def test_rollback_swaps_the_pointers_and_logs_the_outcome(flow):
    home, _ = flow
    supervisor = FakeSupervisor(home)
    supervisor.set_current("R-OLD")
    supervisor.set_current("R-NEW")
    supervisor.do_rollback("conv1")

    assert supervisor.current() == "R-OLD" and supervisor.previous() == "R-NEW"
    assert supervisor.spawns == ["R-OLD"]
    assert [s for s, _ in stages(home)] == ["rollback", "done"]


def test_rollback_restores_the_snapshot_when_the_schema_advanced(flow):
    home, _ = flow
    snapshot(home)  # taken at user_version 4
    make_state(home, version=9)
    supervisor = FakeSupervisor(home)
    supervisor.set_current("R-OLD")
    supervisor.set_current("R-NEW")
    supervisor.do_rollback()
    assert user_version(home / "state.db") == 4
