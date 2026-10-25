"""Listener supervision (03 §5.7): the backend keeps one `loom listen` child alive while
`voice.enabled` is on.

- Started only when `voice.enabled` and the backend is not a `--fake` serve (whose model
  client is the `FakeModelClient`; tests use the same client, so no test ever starts a
  real listener through `install`/`start`).
- `sys.executable -m loom listen --url http://<bind>:<port>` with `LOOM_HOME` and, when
  `general.lan_token` is set, `LOOM_TOKEN` in the environment — never on argv. Output is
  appended to `APP_HOME/logs/voice-listener.log`.
- The child goes in a kill-on-close Job Object (`processes.create_job`), so it can never
  outlive the backend; POSIX falls back to killing it on stop.
- Exit 0 is deliberate (unsupported platform, another listener holds the mutex, an
  invalid hotkey): the supervisor waits for the next settings change. Any other exit is
  restarted after a backoff that doubles from 1 s to 60 s, reset once a child has run
  for a minute.
- Any `voice.*` (or `general.lan_token`) settings change restarts it at once; turning
  voice off stops it; backend shutdown stops it.
- The spawn is a seam (`Supervisor.spawn`): tests pass a fake child. Nothing here feeds
  `/api/system/health`: a broken listener is a status line, not an unhealthy backend.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from typing import Any, Awaitable, Callable, Protocol

from .. import processes

log = logging.getLogger(__name__)

LOG_NAME = "voice-listener.log"
LOG_MAX_BYTES = 1_000_000  # the log is truncated at a spawn once it passes this
RESTART_KEYS_PREFIX = "voice."
RESTART_KEYS = {"general.lan_token"}


class Child(Protocol):
    pid: int | None

    async def wait(self) -> int: ...
    def kill(self) -> None: ...


Spawn = Callable[[list[str], dict[str, str]], Awaitable[Child]]


def is_fake_serve(app: Any) -> bool:
    from ..models.fake import FakeModelClient

    return isinstance(app.models, FakeModelClient)


def listener_url(app: Any) -> str:
    host = str(app.bind_host or app.settings.get("general.bind_host") or "127.0.0.1")
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    port = int(app.bind_port or app.settings.get("general.port"))
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def listener_command(app: Any) -> tuple[list[str], dict[str, str]]:
    argv = [sys.executable, "-m", "loom", "listen", "--url", listener_url(app)]
    env = dict(os.environ)
    env.pop("LOOM_TOKEN", None)
    env.pop("LOOM_URL", None)
    env["LOOM_HOME"] = str(app.home)
    env["PYTHONUTF8"] = "1"
    token = str(app.settings.get("general.lan_token") or "")
    if token:
        env["LOOM_TOKEN"] = token
    return argv, env


class _ProcessChild:
    """An asyncio subprocess in a kill-on-close job."""

    def __init__(self, proc: asyncio.subprocess.Process, job: int | None):
        self.proc = proc
        self.job = job
        self.pid: int | None = proc.pid

    async def wait(self) -> int:
        code = await self.proc.wait()
        processes.close(self.job)
        self.job = None
        return int(code)

    def kill(self) -> None:
        if self.job is not None:
            processes.terminate_job(self.job)
        try:
            self.proc.kill()
        except (ProcessLookupError, OSError):
            pass


def make_spawn(app: Any) -> Spawn:
    async def spawn(argv: list[str], env: dict[str, str]) -> Child:
        logs = app.logs_dir
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / LOG_NAME
        try:
            if path.stat().st_size > LOG_MAX_BYTES:
                path.unlink()
        except OSError:
            pass
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        with open(path, "ab") as fh:
            proc = await asyncio.create_subprocess_exec(
                *argv, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=fh, stderr=asyncio.subprocess.STDOUT, **kwargs,
            )
        job = processes.create_job(kill_on_close=True)
        if job is not None and not processes.assign(job, proc.pid):
            processes.close(job)
            job = None
        return _ProcessChild(proc, job)

    return spawn


class Supervisor:
    min_delay = 1.0
    max_delay = 60.0
    healthy_after = 60.0  # a child that ran this long resets the backoff

    def __init__(self, app: Any, *, spawn: Spawn | None = None, allowed: bool | None = None):
        self.app = app
        self.spawn: Spawn = spawn or make_spawn(app)
        self.allowed = (not is_fake_serve(app)) if allowed is None else allowed
        self.child: Child | None = None
        self.task: asyncio.Task | None = None
        self.restarts = 0  # unexpected exits restarted (status/debugging)
        self.last_exit: int | None = None
        self._wake: asyncio.Event | None = None

    # -- state -------------------------------------------------------------------
    def wanted(self) -> bool:
        return self.allowed and not self.app.shutting_down and bool(self.app.settings.get("voice.enabled"))

    def state(self) -> dict[str, Any]:
        child = self.child
        return {"running": child is not None, "pid": child.pid if child is not None else None}

    # -- lifecycle -----------------------------------------------------------------
    def on_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "settings.changed":
            return
        keys = (event.get("data") or {}).get("keys") or []
        if any(str(k).startswith(RESTART_KEYS_PREFIX) or k in RESTART_KEYS for k in keys):
            self.poke()

    def poke(self) -> None:
        """Re-evaluate now: a running child is replaced (or stopped, when voice is off)."""
        if self._wake is not None:
            self._wake.set()

    async def start(self) -> None:
        self._wake = asyncio.Event()
        self.app.bus.listen(self.on_event)
        self.task = asyncio.get_running_loop().create_task(self._run(), name="voice-listener")

    async def stop(self) -> None:
        self.app.bus.unlisten(self.on_event)
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._kill()

    async def _kill(self) -> None:
        child, self.child = self.child, None
        if child is None:
            return
        try:
            child.kill()
        except Exception as exc:  # pragma: no cover - platform dependent
            log.debug("listener kill failed: %s", exc)
        try:
            await asyncio.wait_for(child.wait(), timeout=10)
        except Exception:
            pass

    async def _wait_wake(self, timeout: float | None) -> None:
        assert self._wake is not None
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def _run(self) -> None:
        assert self._wake is not None
        delay = self.min_delay
        while True:
            self._wake.clear()
            if not self.wanted():
                await self._wait_wake(None)
                continue
            argv, env = listener_command(self.app)
            try:
                child = await self.spawn(argv, env)
            except Exception as exc:
                log.error("voice listener could not start: %s", exc)
                await self._wait_wake(delay)
                delay = min(delay * 2, self.max_delay)
                continue
            self.child = child
            began = time.monotonic()
            log.info("voice listener started (pid %s)", child.pid)
            exit_wait = asyncio.ensure_future(child.wait())
            wake_wait = asyncio.ensure_future(self._wake.wait())
            try:
                await asyncio.wait({exit_wait, wake_wait}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                wake_wait.cancel()
            if not exit_wait.done():
                # Woken: a settings change or voice turned off. Replace or stop the child.
                exit_wait.cancel()
                await self._kill()
                delay = self.min_delay
                continue
            self.child = None
            code = exit_wait.result()
            self.last_exit = code
            if code == 0:
                log.info("voice listener exited 0; waiting for a settings change")
                await self._wait_wake(None)
                delay = self.min_delay
                continue
            if time.monotonic() - began >= self.healthy_after:
                delay = self.min_delay
            log.warning("voice listener exited %s; restarting in %.0f s", code, delay)
            self.restarts += 1
            await self._wait_wake(delay)
            delay = min(delay * 2, self.max_delay)
