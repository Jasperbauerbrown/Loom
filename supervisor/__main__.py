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

Promotion here is only the *process* half: stop, switch the pointer, start, watch
health, revert if it does not hold. The worktree/snapshot/probe steps of 03 §15.2 are
the self-modification package's; they have named seams below that raise until it lands,
and nothing calls them yet.

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
import signal
import socketserver
import sqlite3
import subprocess
import sys
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
    """The interpreter that runs the backend. In development that is the one running
    this supervisor, which by construction can import `loom`. A promoted release will
    get its own virtualenv; the self-modification package returns its python here."""
    return sys.executable


# ---------------------------------------------------------------------------------
# seams for the self-modification package (03 §15.2). Not called yet.
# ---------------------------------------------------------------------------------
def snapshot(home: Path) -> Path:
    raise NotImplementedError("snapshot() lands with the self-modification package (03 §15.2 step 5)")


def create_release(home: Path, ref: str) -> str:
    raise NotImplementedError("create_release() lands with the self-modification package (03 §15.2 step 6)")


def probe(home: Path, release: str, port: int) -> bool:
    raise NotImplementedError("probe() lands with the self-modification package (03 §15.2 step 7)")


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
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (OSError, ValueError):
                pass

    # -- health -------------------------------------------------------------------
    def healthy(self, timeout: float = 3.0) -> bool:
        url = f"http://127.0.0.1:{self.port}{HEALTH_PATH}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                if response.status != 200:
                    return False
                return bool(json.loads(response.read().decode("utf-8")).get("ok"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return False

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
            self.do_promote(str(command.get("ref") or ""))
        elif action == "rollback":
            self.do_rollback()

    def do_promote(self, ref: str) -> None:
        """Process mechanics only: point `current` at `ref`, restart, watch, revert if it
        does not hold. Building the release (worktree, deps, UI, probe) is the
        self-modification package's job; this refuses rather than pretends."""
        release_dir = self.home / "releases" / ref
        if not release_dir.is_dir():
            self.last_error = f"promote: no release at {release_dir}; build it first (03 §15.2 steps 5–7)"
            print(f"supervisor: {self.last_error}", flush=True)
            return
        before = self.current()
        self.kill_child()
        self.set_current(ref)
        self.spawn()
        seconds = int(read_setting(self.home, "system.promotion_watchdog_seconds", DEFAULT_WATCHDOG_SECONDS))
        if self.watchdog(seconds):
            self.last_error = ""
            print(f"supervisor: promoted {ref}", flush=True)
            return
        print(f"supervisor: {ref} failed the watchdog — reverting", flush=True)
        self.kill_child()
        if before:
            (self.home / "current").write_text(before, encoding="utf-8")
        else:
            (self.home / "current").unlink(missing_ok=True)
        self.promoted_at = None
        self.last_error = f"promotion of {ref} failed the {seconds}s watchdog; reverted"
        self.spawn()

    def do_rollback(self) -> None:
        target = self.previous()
        if not target:
            self.last_error = "rollback: no previous release recorded"
            print(f"supervisor: {self.last_error}", flush=True)
            return
        current = self.current()
        self.kill_child()
        (self.home / "current").write_text(target, encoding="utf-8")
        if current:
            (self.home / "previous").write_text(current, encoding="utf-8")
        self.promoted_at = time.time()
        self.crash_times.clear()
        self.spawn()
        print(f"supervisor: rolled back to {target}", flush=True)

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
                    supervisor.commands.put({"action": "promote", "ref": ref})
                elif path == "/rollback":
                    supervisor.commands.put({"action": "rollback"})
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
