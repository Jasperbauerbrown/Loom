"""Supervisor logic as pure functions: backoff, the crash window, the token file.

Nothing here spawns a process — the parts that do are thin wrappers around subprocess,
and a test that starts backends would be slow and flaky for no extra coverage."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys

import pytest

from supervisor.__main__ import (
    BACKOFF_CAP,
    Supervisor,
    backoff_for,
    crashes_in_window,
    ensure_token,
    python_for,
    read_setting,
    should_rollback,
)


def test_backoff_doubles_and_caps():
    assert [backoff_for(n) for n in range(0, 7)] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
    assert backoff_for(100) == BACKOFF_CAP


def test_crashes_in_window_counts_only_recent_ones():
    now = 10_000.0
    times = [now - 5, now - 60, now - 59 * 60, now - 61 * 60]
    assert crashes_in_window(times, now, 10) == 2
    assert crashes_in_window(times, now, 60) == 3
    assert crashes_in_window([], now, 10) == 0


def test_rollback_only_for_a_fresh_release_over_the_threshold():
    now = 10_000.0
    crashes = [now - i for i in range(5)]
    assert should_rollback(crashes, now, promoted_at=now - 60, max_crashes=5, window_minutes=10) is True
    # not enough crashes
    assert should_rollback(crashes[:4], now, promoted_at=now - 60, max_crashes=5, window_minutes=10) is False
    # nothing was promoted: an old release crashing is a machine problem, not a bad build
    assert should_rollback(crashes, now, promoted_at=None, max_crashes=5, window_minutes=10) is False
    # promoted long ago
    assert should_rollback(crashes, now, promoted_at=now - 3600, max_crashes=5, window_minutes=10) is False


def test_token_file_is_random_stable_and_owner_only(tmp_path):
    token = ensure_token(tmp_path)
    assert len(token) == 64 and int(token, 16) >= 0
    assert ensure_token(tmp_path) == token  # stable across calls
    path = tmp_path / "supervisor.token"
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if os.name != "nt":  # Windows chmod cannot express group/other bits
        assert mode == 0o600
    assert ensure_token(tmp_path / "fresh") != token


def test_read_setting_falls_back_without_a_database(tmp_path):
    assert read_setting(tmp_path, "general.port", 8500) == 8500
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO settings VALUES ('general.port', ?, 'now')", (json.dumps(8600),))
    conn.commit()
    conn.close()
    assert read_setting(tmp_path, "general.port", 8500) == 8600
    assert read_setting(tmp_path, "absent.key", "fallback") == "fallback"


def test_release_pointer_records_the_previous_release(tmp_path):
    supervisor = Supervisor(tmp_path, 8500)
    assert supervisor.current() is None and supervisor.previous() is None
    supervisor.set_current("r1")
    assert supervisor.current() == "r1" and supervisor.previous() is None
    supervisor.set_current("r2")
    assert supervisor.current() == "r2" and supervisor.previous() == "r1"
    assert supervisor.promoted_at is not None


def test_workdir_is_the_repo_in_dev_mode_and_the_release_otherwise(tmp_path):
    supervisor = Supervisor(tmp_path, 8500)
    cwd, command = supervisor.workdir()
    assert (cwd / "pyproject.toml").exists()
    # The interpreter running the supervisor is the one that can import `loom`; never
    # `uv run`, whose module is absent from the venv python.
    assert command[:4] == [sys.executable, "-m", "loom", "serve"]
    assert command[4:6] == ["--port", "8500"]
    assert "--no-supervisor" in command and "--no-browser" in command
    assert "uv" not in command

    supervisor.set_current("r1")
    cwd, command = supervisor.workdir()
    assert cwd == tmp_path / "releases" / "r1"
    assert command[:4] == [python_for(cwd), "-m", "loom", "serve"]


def test_extra_args_are_forwarded_to_the_backend(tmp_path):
    _, command = Supervisor(tmp_path, 8500, ["--fake", "--host", "0.0.0.0"]).workdir()
    assert command[-3:] == ["--fake", "--host", "0.0.0.0"]


def test_cli_builds_the_supervisor_argv_with_this_interpreter():
    from loom import cli

    args = cli.build_parser().parse_args(["serve", "--port", "8599", "--fake"])
    argv = cli.supervisor_argv(args)
    assert argv[:3] == [sys.executable, "-m", "supervisor"]
    assert "--port" in argv and "8599" in argv and "--fake" in argv


def test_promote_refuses_a_ref_that_does_not_resolve(tmp_path):
    """An empty bare clone stands in for `releases/src.git` so nothing is fetched: the
    point is that an unresolvable ref stops before anything moves."""
    src = tmp_path / "releases" / "src.git"
    src.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(src)], capture_output=True, check=True)
    supervisor = Supervisor(tmp_path, 8500)
    supervisor.do_promote("nope")
    assert "does not resolve to a commit" in supervisor.last_error
    assert supervisor.current() is None and supervisor.process is None
    last = json.loads((tmp_path / "promote.log").read_text(encoding="utf-8").strip().splitlines()[-1])
    assert last["stage"] == "failed" and last["ok"] is False and last["ref"] == "nope"


def test_rollback_without_a_previous_release_reports_rather_than_guesses(tmp_path):
    supervisor = Supervisor(tmp_path, 8500)
    supervisor.do_rollback()
    assert supervisor.last_error == "rollback: no previous release recorded"


def test_uv_is_taken_from_path_before_this_interpreter(monkeypatch):
    """The supervisor's interpreter is whatever launched it — under `uv run`, a
    virtualenv with no `uv` module — so PATH wins (2026-09-28)."""
    import shutil

    from supervisor.__main__ import uv_command

    monkeypatch.setattr(shutil, "which", lambda name: "C:/tools/uv.exe" if name == "uv" else None)
    assert uv_command() == ["C:/tools/uv.exe"]
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert uv_command() == [sys.executable, "-m", "uv"]


def test_self_modification_seams_are_implemented(tmp_path):
    """The three seams of 03 §15.2 are real now; their behaviour is exercised in
    tests/test_supervisor_release.py."""
    from supervisor.__main__ import create_release, probe, snapshot

    assert snapshot(tmp_path).is_dir()
    with pytest.raises(Exception):  # no repo to clone from, no release to probe
        create_release(tmp_path, "ref", origin=tmp_path / "absent")
    with pytest.raises(Exception):
        probe(tmp_path, "absent-release", 1)


def test_status_shape(tmp_path):
    status = Supervisor(tmp_path, 8500).status()
    assert set(status) == {
        "pid", "release", "previous", "uptime_s", "crashes", "crashes_in_window",
        "backoff_s", "healthy", "last_error", "port",
    }
    assert status["pid"] is None and status["port"] == 8500
