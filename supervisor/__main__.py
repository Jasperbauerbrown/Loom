"""The supervisor (03 §3.1). Stdlib only — it never imports `loom`, so a backend that
cannot even start still has something alive to restart it and roll it back.

    python -m supervisor [--home H] [--port 8500]

It runs the release named by `H/current` from `H/releases/<id>`, or, when there is no
`current` (development), the repo this file sits in via `python -m uv run loom serve`.
Crashes restart with exponential backoff 1s → 60s. `auto_rollback_crashes` crashes
inside `auto_rollback_window_minutes` on a release promoted within that window rewrite
`current` from `previous` and restart — the one automatic recovery, because a release
that cannot stay up cannot be fixed from inside itself.

A loopback HTTP server on `port + 1` answers `GET /status`, `POST /restart`,
`POST /promote {ref}` and `POST /rollback`; every request must carry
`X-Supervisor-Token` matching `H/supervisor.token` (random, owner-only).

Promotion is the whole of 03 §15.2 steps 5–10: snapshot, build the release worktree,
probe it on a scratch home, swap the pointer, restart, watch health, and restore
everything (pointers, and the database when migrations ran) if it does not hold. Every
stage appends a JSON line to `H/promote.log`, which the backend tails and republishes
as `promote.progress`; the backend cannot be told directly because it is the thing
being restarted.

Windows note (decided 2026-09-20): the child is started with CREATE_NEW_PROCESS_GROUP
and its tree is killed with `taskkill /T /F`. A Job Object would be tidier but needs
ctypes plumbing in a file whose whole point is to be small and obviously correct; the
design doc said Job Object and now says this.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import queue
import re
import shutil
import signal
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BACKOFF_START = 1.0
BACKOFF_CAP = 60.0
DEFAULT_PORT = 8500
DEFAULT_CRASHES = 5
DEFAULT_WINDOW_MINUTES = 10
DEFAULT_WATCHDOG_SECONDS = 120
HEALTH_PATH = "/api/system/health"


# ---------------------------------------------------------------------------------
# pure helpers (unit-tested; no processes involved)
# ---------------------------------------------------------------------------------
def backoff_for(attempt: int, start: float = BACKOFF_START, cap: float = BACKOFF_CAP) -> float:
    """1, 2, 4, … capped. `attempt` is 0 for the first restart."""
    if attempt <= 0:
        return start
    return min(cap, start * (2.0 ** attempt))


def crashes_in_window(times: list[float], now: float, window_minutes: int) -> int:
    floor = now - window_minutes * 60
    return sum(1 for t in times if t >= floor)


def should_rollback(
    crash_times: list[float],
    now: float,
    promoted_at: float | None,
    max_crashes: int,
    window_minutes: int,
) -> bool:
    """Roll back only a *fresh* release: an old release crashing is a machine problem,
    and swapping it out would lose state without fixing anything."""
    if promoted_at is None:
        return False
    if now - promoted_at > window_minutes * 60:
        return False
    return crashes_in_window(crash_times, now, window_minutes) >= max_crashes


def read_setting(home: Path, key: str, default: Any) -> Any:
    """Read one row from the backend's settings table without importing the backend."""
    state = home / "state.db"
    if not state.exists():
        return default
    try:
        conn = sqlite3.connect(f"file:{state}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return default
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return default


def ensure_token(home: Path) -> str:
    """Random 32-byte hex in `H/supervisor.token`, owner-only where the OS supports it."""
    path = home / "supervisor.token"
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            _lock_down(path)
            return token
    token = os.urandom(32).hex()
    home.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    _lock_down(path)
    return token


def _lock_down(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def repo_root() -> Path:
    """The checkout this supervisor lives in (development mode)."""
    return Path(__file__).resolve().parents[1]


def python_for(release_dir: Path | None) -> str:
    """The interpreter that runs the backend: a release's own virtualenv (written by
    `uv sync` in `create_release`) when it has one, otherwise the interpreter running
    this supervisor, which by construction can import `loom`."""
    if release_dir is not None:
        for relative in ("Scripts/python.exe", "bin/python"):
            candidate = Path(release_dir) / ".venv" / relative
            if candidate.exists():
                return str(candidate)
    return sys.executable


# ---------------------------------------------------------------------------------
# release building: 03 §15.2 steps 5–7. Stdlib only, Windows-safe, no `loom` import.
# ---------------------------------------------------------------------------------
class ReleaseError(RuntimeError):
    """A release could not be built, probed or swapped. One class, one behaviour: the
    caller logs it as a `failed` stage and restores whatever it had already changed."""


def run_command(command: list[str], cwd: Path | str | None = None, timeout: float = 1800.0,
                env: dict[str, str] | None = None) -> tuple[bool, str]:
    """Run a command; return (ok, combined output). Never raises for a non-zero exit —
    failure is an expected input here."""
    try:
        proc = subprocess.run(
            command, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{command[0]}: {exc}"
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def git(*args: str, cwd: Path | str | None = None, timeout: float = 900.0) -> tuple[bool, str]:
    return run_command(["git", *args], cwd=cwd, timeout=timeout)


def kill_tree(pid: int) -> None:
    """Kill a process and everything it started (decided 2026-09-20: `taskkill /T /F`
    on Windows, process groups elsewhere)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (OSError, ValueError):
            pass


def health_ok(port: int, timeout: float = 3.0) -> bool:
    url = f"http://127.0.0.1:{port}{HEALTH_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return False
            return bool(json.loads(response.read().decode("utf-8")).get("ok"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def copy_database(source: Path, target: Path) -> bool:
    """A consistent copy through the sqlite backup API (WAL and all). A missing source
    is not an error: a home with no state.db is a first run."""
    if not source.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30.0)
    try:
        dst = sqlite3.connect(str(target), timeout=30.0)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return True


def user_version(state_db: Path) -> int:
    """`PRAGMA user_version` — the migration number. Used to decide whether a failed
    promotion left the database ahead of the release we are returning to."""
    if not state_db.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5.0)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


# -- step 5: snapshot ---------------------------------------------------------------
def snapshot(home: Path) -> Path:
    """`snapshots/<ts>/state.db` plus a `pre-<ts>` tag in the vault, then prune to
    `system.snapshot_retention`. Persist before act: this runs before anything moves."""
    ts = stamp()
    folder = home / "snapshots" / ts
    folder.mkdir(parents=True, exist_ok=True)
    copy_database(home / "state.db", folder / "state.db")
    vault = home / "vault"
    if (vault / ".git").exists():
        git("tag", "-f", f"pre-{ts}", cwd=vault, timeout=60)
    prune_snapshots(home, int(read_setting(home, "system.snapshot_retention", 10)))
    return folder


def prune_snapshots(home: Path, keep: int) -> list[str]:
    """Oldest first by name (the names are timestamps). Returns what was removed."""
    root = home / "snapshots"
    if not root.is_dir():
        return []
    folders = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name)
    removed = []
    for folder in folders[: max(0, len(folders) - max(1, keep))]:
        shutil.rmtree(folder, ignore_errors=True)
        removed.append(folder.name)
    return removed


def latest_snapshot(home: Path) -> Path | None:
    root = home / "snapshots"
    if not root.is_dir():
        return None
    folders = sorted([p for p in root.iterdir() if (p / "state.db").exists()], key=lambda p: p.name)
    return folders[-1] if folders else None


def restore_snapshot(home: Path, folder: Path) -> bool:
    """Put a snapshot back. The backend is not running when this is called, so the
    `-wal`/`-shm` siblings of the live database are stale and go with it."""
    source = folder / "state.db"
    if not source.exists():
        return False
    state = home / "state.db"
    for suffix in ("-wal", "-shm"):
        Path(str(state) + suffix).unlink(missing_ok=True)
    shutil.copy2(source, state)
    return True


# -- step 6: create the release worktree --------------------------------------------
def ensure_src_clone(home: Path, origin: Path | str | None = None) -> Path:
    """`releases/src.git`: one bare clone of the assistant's own repo, the origin being
    the checkout the supervisor runs from in development."""
    src = home / "releases" / "src.git"
    if (src / "HEAD").exists():
        return src
    source = Path(origin) if origin else repo_root()
    src.parent.mkdir(parents=True, exist_ok=True)
    ok, out = git("clone", "--bare", str(source), str(src))
    if not ok:
        raise ReleaseError(f"could not clone {source} into {src}: {out.strip()}")
    git("remote", "set-url", "origin", str(source), cwd=src)
    git("config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*", cwd=src)
    return src


def create_release(home: Path, ref: str, origin: Path | str | None = None,
                   deps: bool = True, build_ui: bool = True) -> str:
    """Fetch, resolve `ref`, add a detached worktree at `releases/<ts>-<sha7>`, install
    its dependencies, and build the UI bundle only when `ui/` actually moved. Returns
    the release id."""
    src = ensure_src_clone(home, origin)
    git("fetch", "--prune", "--tags", "origin", cwd=src)
    git("fetch", "origin", ref, cwd=src)  # a bare sha, or a branch that is not a head here
    ok, out = git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=src)
    if not ok:
        raise ReleaseError(f"{ref} does not resolve to a commit in {src}: {out.strip()}")
    sha = out.strip().splitlines()[-1].strip()
    release = f"{stamp()}-{sha[:7]}"
    dest = home / "releases" / release
    ok, out = git("worktree", "add", "--detach", str(dest), sha, cwd=src)
    if not ok:
        raise ReleaseError(f"could not create the worktree {dest}: {out.strip()}")
    if deps:
        install_deps(dest)
    if build_ui and ui_needs_building(home, src, dest, sha):
        build_ui_bundle(dest)
    return release


def uv_command() -> list[str]:
    """How this supervisor runs uv: the `uv` on PATH when there is one, else this
    interpreter's `uv` module. The supervisor's interpreter is whatever launched
    `loom serve` — under `uv run` that is the project virtualenv, which has no `uv`
    module unless someone installed one there, and a `uv sync` can prune it again
    (2026-09-28: the third promotion failed "No module named uv" in this very step)."""
    found = shutil.which("uv")
    return [found] if found else [sys.executable, "-m", "uv"]


def install_deps(release_dir: Path) -> None:
    """`uv sync --frozen`, falling back to a plain sync when the lockfile moved."""
    output = ""
    for args in (["sync", "--frozen"], ["sync"]):
        ok, output = run_command([*uv_command(), *args], cwd=release_dir)
        if ok:
            return
    raise ReleaseError(f"uv sync failed in {release_dir}: {output.strip()[-2000:]}")


DEFAULT_TEST_TIMEOUT_S = 1800
_SUMMARY = re.compile(r"\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed)[^\n]*? in [\d.]+s(?: \([^)]*\))?")


def pytest_summary(output: str) -> str:
    """The suite's own last word ("889 passed, 1 warning in 553.96s (0:09:13)"), or ""
    when the output has none — which means the runner never got as far as pytest."""
    found = _SUMMARY.findall(output or "")
    return found[-1].strip() if found else ""


def run_suite(release_dir: Path, timeout_s: float = DEFAULT_TEST_TIMEOUT_S) -> tuple[bool, str]:
    """`uv run pytest -q -x` in the release (03 §15.2 step 7): the exact tree, venv and
    interpreter that will run if it passes. The child gets the release's own `src` on
    PYTHONPATH and no `VIRTUAL_ENV`, so it imports the candidate and not whatever
    launched this supervisor. Returns (ok, output); a timeout is a failure with its
    reason as the output."""
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env["PYTHONPATH"] = str(Path(release_dir) / "src")
    return run_command([*uv_command(), "run", "pytest", "-q", "-x"], cwd=release_dir, timeout=timeout_s, env=env)


def current_sha(home: Path) -> str | None:
    """The commit the live release (or the development checkout) sits on."""
    pointer = home / "current"
    release = pointer.read_text(encoding="utf-8").strip() if pointer.exists() else ""
    directory = home / "releases" / release if release else repo_root()
    if not directory.is_dir():
        return None
    ok, out = git("rev-parse", "HEAD", cwd=directory, timeout=60)
    return out.strip() if ok else None


def newest_mtime(path: Path) -> float:
    if not path.exists():
        return 0.0
    newest = path.stat().st_mtime
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != "node_modules"]
        for name in files:
            try:
                newest = max(newest, os.stat(os.path.join(root, name)).st_mtime)
            except OSError:
                pass
    return newest


def ui_needs_building(home: Path, src: Path, release_dir: Path, sha: str) -> bool:
    """`ui-dist/` is committed, so a fresh worktree usually already carries the bundle.
    Build only when `ui/` moved since the live release and the bundle is missing or
    older than the sources."""
    base = current_sha(home)
    if base:
        ok, out = git("diff", "--name-only", base, sha, "--", "ui/", cwd=src, timeout=120)
        if ok and not out.strip():
            return False
    dist = release_dir / "ui-dist"
    if not dist.is_dir() or not any(dist.iterdir()):
        return True
    return newest_mtime(release_dir / "ui") > newest_mtime(dist)


def build_ui_bundle(release_dir: Path) -> None:
    npm = shutil.which("npm")
    if npm is None:
        raise ReleaseError("npm is not on PATH; this release changes ui/ and cannot be built")
    for args in (["--prefix", "ui", "ci"], ["--prefix", "ui", "run", "build"]):
        ok, out = run_command([npm, *args], cwd=release_dir)
        if not ok:
            raise ReleaseError(f"npm {' '.join(args)} failed: {out.strip()[-2000:]}")


# -- step 7: probe -------------------------------------------------------------------
def probe(home: Path, release: str, port: int, timeout_s: float = 60.0) -> bool:
    """Start the release against a *copy* of the database in a scratch LOOM_HOME and
    wait for health. Migrations run on the copy, so a release that cannot migrate is
    caught before anything touches live state."""
    release_dir = home / "releases" / release
    if not release_dir.is_dir():
        raise ReleaseError(f"no release at {release_dir}")
    scratch = Path(tempfile.mkdtemp(prefix="loom-probe-"))
    process: subprocess.Popen | None = None
    try:
        copy_database(home / "state.db", scratch / "state.db")
        env = dict(os.environ)
        env["LOOM_HOME"] = str(scratch)
        src = release_dir / "src"
        if src.is_dir():
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(src), env.get("PYTHONPATH", "")]))
        command = [
            python_for(release_dir), "-m", "loom", "serve",
            "--no-supervisor", "--no-browser", "--port", str(port),
        ]
        kwargs: dict[str, Any] = {
            "cwd": str(release_dir), "env": env,
            "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            raise ReleaseError(f"could not start the probe: {exc}") from None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if process.poll() is not None:
                return False
            if health_ok(port, timeout=2.0):
                return True
            time.sleep(1.0)
        return False
    finally:
        if process is not None and process.poll() is None:
            kill_tree(process.pid)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(scratch, ignore_errors=True)


# -- progress log --------------------------------------------------------------------
PROGRESS_LOG = "promote.log"
TERMINAL_STAGES = ("done", "failed")


def log_progress(home: Path, stage: str, ok: bool, message: str, ref: str = "",
                 conversation_id: str = "") -> dict[str, Any]:
    """One JSON line per stage in `APP_HOME/promote.log`. The backend tails it and
    republishes each line as a `promote.progress` event (03 §15.2 step 10) — a file,
    because the backend being reported on is restarted halfway through."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stage": stage,
        "ok": bool(ok),
        "message": message,
        "ref": ref,
        "conversation_id": conversation_id,
    }
    try:
        home.mkdir(parents=True, exist_ok=True)
        with (home / PROGRESS_LOG).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError as exc:  # the log must never take a promotion down
        print(f"supervisor: could not write {PROGRESS_LOG}: {exc}", flush=True)
    print(f"supervisor: {stage}: {message}", flush=True)
    return entry


# ---------------------------------------------------------------------------------
# the supervisor
# ---------------------------------------------------------------------------------
class Supervisor:
    def __init__(self, home: Path, port: int, extra_args: list[str] | None = None):
        self.home = home
        self.port = port
        self.extra_args = list(extra_args or [])  # forwarded verbatim to `loom serve`
        self.token = ensure_token(home)
        self.process: subprocess.Popen | None = None
        self.started_at: float = 0.0
        self.promoted_at: float | None = None
        self.crash_times: list[float] = []
        self.backoff: float = 0.0
        self.commands: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.stopping = False
        self.last_error = ""

    # -- release pointer ---------------------------------------------------------
    def current(self) -> str | None:
        path = self.home / "current"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").strip() or None

    def previous(self) -> str | None:
        path = self.home / "previous"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").strip() or None

    def set_current(self, release: str) -> None:
        old = self.current()
        if old:
            (self.home / "previous").write_text(old, encoding="utf-8")
        (self.home / "current").write_text(release, encoding="utf-8")
        self.promoted_at = time.time()
        self.crash_times.clear()

    def workdir(self) -> tuple[Path, list[str]]:
        """Where the backend runs and the command that runs it.

        Always `<python> -m loom serve --no-supervisor`, never `uv run`: the supervisor
        was itself started from an interpreter that can import `loom`, and that is the
        one interpreter we know works. `python_for` is the seam the self-modification
        package fills in when a release gets its own virtualenv."""
        release = self.current()
        directory = self.home / "releases" / release if release else repo_root()
        command = [
            python_for(directory if release else None),
            "-m", "loom", "serve",
            "--port", str(self.port),
            "--no-supervisor",
            "--no-browser",
        ]
        return directory, command + list(self.extra_args)

    # -- child process -----------------------------------------------------------
    def spawn(self) -> None:
        cwd, command = self.workdir()
        env = dict(os.environ)
        env["LOOM_HOME"] = str(self.home)
        src = cwd / "src"
        if src.is_dir():  # development checkout without an editable install
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(src), env.get("PYTHONPATH", "")]))
        kwargs: dict[str, Any] = {"cwd": str(cwd), "env": env}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(command, **kwargs)
        self.started_at = time.time()
        print(f"supervisor: backend pid {self.process.pid} in {cwd}", flush=True)

    def kill_child(self, grace: float = 10.0) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            self.process = None
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.kill_tree(process.pid)
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        self.process = None

    @staticmethod
    def kill_tree(pid: int) -> None:
        kill_tree(pid)

    # -- health -------------------------------------------------------------------
    def healthy(self, timeout: float = 3.0) -> bool:
        return health_ok(self.port, timeout)

    def watchdog(self, seconds: int) -> bool:
        """Health must be reachable and stay ok for the whole window."""
        deadline = time.time() + seconds
        reached = False
        while time.time() < deadline:
            if self.process is None or self.process.poll() is not None:
                return False
            if self.healthy():
                reached = True
            elif reached:
                return False
            time.sleep(2.0)
        return reached

    # -- main loop -----------------------------------------------------------------
    def run(self) -> int:
        server = self.start_control_server()
        attempt = 0
        try:
            self.spawn()
            while not self.stopping:
                self.drain_commands()
                if self.stopping:
                    break
                process = self.process
                if process is None:
                    self.spawn()
                    attempt = 0
                    continue
                code = process.poll()
                if code is None:
                    time.sleep(0.5)
                    continue
                if self.stopping:
                    break
                print(f"supervisor: backend exited with {code}", flush=True)
                self.crash_times.append(time.time())
                self.process = None
                if self.maybe_rollback():
                    attempt = 0
                    continue
                self.backoff = backoff_for(attempt)
                attempt += 1
                print(f"supervisor: restarting in {self.backoff:.0f}s", flush=True)
                if self.sleep_interruptible(self.backoff):
                    break
                self.backoff = 0.0
                self.spawn()
        except KeyboardInterrupt:
            pass
        finally:
            self.stopping = True
            self.kill_child()
            server.shutdown()
        return 0

    def sleep_interruptible(self, seconds: float) -> bool:
        """Sleep, but wake for a command. Returns True when asked to stop."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.drain_commands()
            if self.stopping or self.process is not None:
                return self.stopping
            time.sleep(0.2)
        return self.stopping

    def maybe_rollback(self) -> bool:
        max_crashes = int(read_setting(self.home, "system.auto_rollback_crashes", DEFAULT_CRASHES))
        window = int(read_setting(self.home, "system.auto_rollback_window_minutes", DEFAULT_WINDOW_MINUTES))
        if not should_rollback(self.crash_times, time.time(), self.promoted_at, max_crashes, window):
            return False
        target = self.previous()
        if not target:
            print("supervisor: crash loop on a fresh release but no previous release to return to", flush=True)
            return False
        print(f"supervisor: crash loop — rolling back to {target}", flush=True)
        (self.home / "current").write_text(target, encoding="utf-8")
        self.promoted_at = None
        self.crash_times.clear()
        self.last_error = f"auto-rolled back to {target} after {max_crashes} crashes"
        log_progress(self.home, "failed", False, self.last_error, target)
        self.spawn()
        return True

    # -- queued commands ------------------------------------------------------------
    def drain_commands(self) -> None:
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                self.execute(command)
            except Exception as exc:  # a bad command must never take the supervisor down
                self.last_error = f"{command.get('action')}: {exc}"
                print(f"supervisor: {self.last_error}", flush=True)

    def execute(self, command: dict[str, Any]) -> None:
        action = command.get("action")
        if action == "restart":
            self.kill_child()
            self.crash_times.clear()
            self.spawn()
        elif action == "stop":
            self.stopping = True
        elif action == "promote":
            self.do_promote(str(command.get("ref") or ""), str(command.get("conversation_id") or ""))
        elif action == "rollback":
            self.do_rollback(str(command.get("conversation_id") or ""))

    def do_promote(self, ref: str, conversation_id: str = "") -> None:
        """03 §15.2 steps 5–10. Snapshot, build, probe, swap, watch — and on any failure
        put back exactly what was changed, in reverse order. Persist before act: the
        snapshot exists before the worktree, `previous` before `current`."""
        home = self.home
        before_current, before_previous = self.current(), self.previous()
        before_version = user_version(home / "state.db")
        snapshot_dir: Path | None = None
        swapped = False

        def stage(name: str, ok: bool, message: str) -> None:
            log_progress(home, name, ok, message, ref, conversation_id)

        try:
            stage("snapshot", True, "snapshotting state.db and tagging the vault")
            snapshot_dir = snapshot(home)
            stage("release", True, f"building a release worktree for {ref}")
            release = create_release(home, ref)
            # The suite runs here, in the release it will be — not in the backend that asked
            # (2026-09-28): that call blocked the desk for the suite's whole duration with no
            # visible progress, and a backend running from a release tested the wrong code.
            timeout_s = float(read_setting(home, "system.promotion_test_timeout_s", DEFAULT_TEST_TIMEOUT_S))
            stage("tests", True, f"running the test suite in {release} (limit {timeout_s:g}s)")
            ok, output = run_suite(home / "releases" / release, timeout_s)
            summary = pytest_summary(output)
            if not ok:
                tail = output.strip()[-1500:]
                if summary:  # pytest ran and said so: a real failure
                    raise ReleaseError(f"the test suite fails in {release}: {summary}\n{tail}")
                raise ReleaseError(f"the test runner could not run in {release}: {tail}")
            stage("tests", True, f"suite passed in {release}: {summary or 'no summary line'}")
            probe_port = self.port + 2
            stage("probe", True, f"probing {release} on port {probe_port}")
            if not probe(home, release, probe_port):
                raise ReleaseError(f"{release} did not answer {HEALTH_PATH} on the probe port")
            stage("swap", True, f"stopping the backend and pointing current at {release}")
            self.kill_child()
            self.set_current(release)  # writes `previous` first, then `current`
            swapped = True
            self.spawn()
            seconds = int(read_setting(home, "system.promotion_watchdog_seconds", DEFAULT_WATCHDOG_SECONDS))
            stage("watchdog", True, f"watching health for {seconds}s")
            if not self.watchdog(seconds):
                raise ReleaseError(f"health did not hold for {seconds}s")
            self.last_error = ""
            stage("done", True, f"promoted {release} ({ref})")
        except Exception as exc:
            self.last_error = f"promote {ref}: {exc}"
            restored = ""
            if swapped:  # nothing before the swap stops the running backend
                restored = self.restore(before_current, before_previous, snapshot_dir, before_version)
            where = before_current or "the development checkout"
            stage("failed", False, f"{exc}; back on {where}{restored}")

    def restore(self, current: str | None, previous: str | None,
                snapshot_dir: Path | None, before_version: int) -> str:
        """Undo a swap: pointers back, the pre-promotion database back when the new
        release migrated it, backend restarted. Returns what to add to the log line."""
        self.kill_child()
        self.write_pointer("current", current)
        self.write_pointer("previous", previous)
        self.promoted_at = None
        self.crash_times.clear()
        note = ""
        if snapshot_dir is not None and user_version(self.home / "state.db") > before_version:
            if restore_snapshot(self.home, snapshot_dir):
                note = f"; restored the snapshot {snapshot_dir.name} (schema had advanced)"
        self.spawn()
        return note

    def write_pointer(self, name: str, value: str | None) -> None:
        path = self.home / name
        if value:
            path.write_text(value, encoding="utf-8")
        else:
            path.unlink(missing_ok=True)

    def do_rollback(self, conversation_id: str = "") -> None:
        """03 §15.4: back to `previous`, restoring the snapshot when the schema moved
        on (state written since is lost; the UI says so before confirming)."""
        home = self.home
        target = self.previous()
        if not target:
            self.last_error = "rollback: no previous release recorded"
            log_progress(home, "failed", False, self.last_error, "", conversation_id)
            return
        current = self.current()
        log_progress(home, "rollback", True, f"pointing current at {target}", target, conversation_id)
        self.kill_child()
        version_before = user_version(home / "state.db")
        self.write_pointer("current", target)
        self.write_pointer("previous", current)
        note = ""
        snapshot_dir = latest_snapshot(home)
        if snapshot_dir is not None and user_version(snapshot_dir / "state.db") < version_before:
            if restore_snapshot(home, snapshot_dir):
                note = f"; restored the snapshot {snapshot_dir.name} (schema had advanced)"
        self.promoted_at = time.time()
        self.crash_times.clear()
        self.last_error = ""
        self.spawn()
        log_progress(home, "done", True, f"rolled back to {target}{note}", target, conversation_id)

    # -- control server ---------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        process = self.process
        return {
            "pid": process.pid if process else None,
            "release": self.current(),
            "previous": self.previous(),
            "uptime_s": int(time.time() - self.started_at) if self.started_at and process else 0,
            "crashes": len(self.crash_times),
            "crashes_in_window": crashes_in_window(
                self.crash_times, time.time(), int(read_setting(self.home, "system.auto_rollback_window_minutes", DEFAULT_WINDOW_MINUTES))
            ),
            "backoff_s": self.backoff,
            "healthy": self.healthy(timeout=1.0),
            "last_error": self.last_error,
            "port": self.port,
        }

    def start_control_server(self) -> socketserver.TCPServer:
        supervisor = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: Any) -> None:  # keep the console for the backend
                pass

            def authorised(self) -> bool:
                return self.headers.get("X-Supervisor-Token", "") == supervisor.token

            def reply(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if not self.authorised():
                    return self.reply(401, {"detail": "bad supervisor token"})
                if self.path.split("?")[0] == "/status":
                    return self.reply(200, supervisor.status())
                self.reply(404, {"detail": "not found"})

            def do_POST(self) -> None:
                if not self.authorised():
                    return self.reply(401, {"detail": "bad supervisor token"})
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    return self.reply(400, {"detail": "expected JSON"})
                path = self.path.split("?")[0]
                if path == "/restart":
                    supervisor.commands.put({"action": "restart"})
                elif path == "/promote":
                    ref = str(payload.get("ref") or "")
                    if not ref:
                        return self.reply(400, {"detail": "ref is required"})
                    supervisor.commands.put({
                        "action": "promote", "ref": ref,
                        "conversation_id": str(payload.get("conversation_id") or ""),
                    })
                elif path == "/rollback":
                    supervisor.commands.put({
                        "action": "rollback",
                        "conversation_id": str(payload.get("conversation_id") or ""),
                    })
                else:
                    return self.reply(404, {"detail": "not found"})
                self.reply(202, {"queued": path.lstrip("/")})

        server = socketserver.TCPServer(("127.0.0.1", self.port + 1), Handler)
        server.allow_reuse_address = True
        threading.Thread(target=server.serve_forever, daemon=True, name="supervisor-control").start()
        return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="supervisor", description="Run and watch the Loom backend.")
    parser.add_argument("--home", help="APP_HOME (default $LOOM_HOME or ~/.loom)")
    parser.add_argument("--port", type=int, help=f"backend port (default {DEFAULT_PORT} or the stored setting)")
    parser.add_argument("--host", help="bind address, forwarded to the backend")
    parser.add_argument("--fake", action="store_true", help="forwarded: run the backend with the fake model client")
    args = parser.parse_args(argv)

    raw_home = args.home or os.environ.get("LOOM_HOME")
    home = Path(raw_home).expanduser() if raw_home else Path.home() / ".loom"
    home.mkdir(parents=True, exist_ok=True)
    port = args.port or int(read_setting(home, "general.port", DEFAULT_PORT))
    extra: list[str] = []
    if args.host:
        extra += ["--host", args.host]
    if args.fake:
        extra.append("--fake")
    return Supervisor(home, port, extra).run()


if __name__ == "__main__":
    raise SystemExit(main())
