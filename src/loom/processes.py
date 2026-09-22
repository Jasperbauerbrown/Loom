"""Background processes (03 §9.6, decided 2026-09-22).

A long-running program the model starts with `shell(background=true)` — a dev server, a
watcher, a bot — is a `processes` row, a child process whose output goes to a log file,
and a watcher task. The row is written before the spawn (persist before act); the log
file lives beside the conversation's artifacts (`proc_<id>.log`) and is pruned with
them; the watcher marks the row when the child ends and posts one message to the owning
conversation so the thread learns about it at its next step.

The child never inherits a pipe: a pipe would break it the moment the backend restarts,
and a program the person asked to keep up must survive a Loom restart. So `stop(app)`
cancels the watchers and leaves children running, and `start(app)` reconciles: a
`running` row whose pid is still alive gets a polling watcher (it is no longer our
child, so it cannot be awaited); one whose pid is gone becomes `lost`; a `starting` row
becomes `failed`. Nothing here restarts a process on its own.

Statuses: starting | running | exited | stopped | failed | lost. Bus events:
`process.started`, `process.exited`, `process.stopped`, `process.failed`,
`process.lost`, each with the row and the owning conversation id.

No new dependency: pids are probed with `ctypes` on Windows and `os.kill(pid, 0)`
elsewhere; trees are killed with `taskkill /T /F` or `killpg`.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from . import db
from .ids import ulid

log = logging.getLogger(__name__)

STATUSES = ("starting", "running", "exited", "stopped", "failed", "lost")
TAIL_CHARS = 600  # of log quoted in the exit message


# -- pid helpers (shared with the shell tool's timeout path) -------------------------

def kill_tree(pid: int | None) -> None:
    """Kill `pid` and every descendant. Best effort; already-dead is fine."""
    if not pid or int(pid) <= 0:
        return
    pid = int(pid)
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10, check=False)
            return
        except Exception as exc:  # pragma: no cover - platform dependent
            log.debug("taskkill failed (%s); falling back to os.kill", exc)
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return
    except Exception:
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def pid_alive(pid: int | None) -> bool:
    """Whether a process with this pid is still running. On Windows a handle opened for
    SYNCHRONIZE is signalled once the process ends, so WaitForSingleObject(h, 0) says
    WAIT_TIMEOUT only while it runs."""
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return int(kernel32.WaitForSingleObject(handle, 0)) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _duration(started_at: str | None, ended_at: str | None = None) -> str:
    try:
        start = _dt.datetime.fromisoformat((started_at or "").replace("Z", "+00:00"))
    except ValueError:
        return "?"
    end = _dt.datetime.fromisoformat(ended_at.replace("Z", "+00:00")) if ended_at else _dt.datetime.now(_dt.timezone.utc)
    seconds = max(0, int((end - start).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m"


def duration(row: dict[str, Any]) -> str:
    """Human wall time a process has run (or ran)."""
    return _duration(row.get("started_at"), row.get("ended_at"))


# -- the store ---------------------------------------------------------------------

class Processes:
    poll_interval: float = 5.0  # seconds between pid probes for a re-attached process

    def __init__(self, app: Any) -> None:
        self.app = app
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    # -- rows ------------------------------------------------------------------
    def _decorate(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        try:
            row["log_bytes"] = os.path.getsize(row["log_path"]) if row.get("log_path") else 0
        except OSError:
            row["log_bytes"] = 0
        return row

    def get(self, process_id: str) -> dict[str, Any] | None:
        return self._decorate(db.row_to_dict(self.app.db.execute("SELECT * FROM processes WHERE id=?", (process_id,)).fetchone()))

    def list(self, conversation_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM processes"
        where, params = [], []
        if conversation_id:
            where.append("conversation_id=?")
            params.append(conversation_id)
        if status:
            where.append("status=?")
            params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at DESC, id DESC"
        return [self._decorate(db.row_to_dict(r)) for r in self.app.db.execute(sql, params).fetchall()]  # type: ignore[misc]

    def _set(self, process_id: str, **fields: Any) -> dict[str, Any] | None:
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self.app.db.execute(f"UPDATE processes SET {cols} WHERE id=?", (*fields.values(), process_id))
        return self.get(process_id)

    def _end(self, process_id: str, status: str, *, exit_code: int | None = None, error: str | None = None) -> dict[str, Any] | None:
        row = self._set(process_id, status=status, exit_code=exit_code, error=error, ended_at=db.now_iso())
        if row is not None:
            self.app.bus.emit(f"process.{status}", row, row["conversation_id"])
        return row

    # -- start / stop --------------------------------------------------------------
    async def start(self, conversation_id: str, command: str, cwd: str, env: dict[str, str] | None = None, title: str = "") -> dict[str, Any]:
        """Insert the row, then spawn. Returns the row, `running` with a pid or `failed`
        with `error`. The caller has already resolved and guarded `cwd`."""
        from .tools.native.shell import resolve_shell  # lazy: shell imports kill_tree from here

        command = (command or "").strip()
        title = (title or "").strip() or command[:60]
        process_id = ulid()
        folder = Path(self.app.artifacts_dir) / str(conversation_id)
        log_path = folder / f"proc_{process_id}.log"
        now = db.now_iso()
        with db.transaction(self.app.db):
            self.app.db.execute(
                "INSERT INTO processes(id, conversation_id, title, command, cwd, status, log_path, started_at) "
                "VALUES (?, ?, ?, ?, ?, 'starting', ?, ?)",
                (process_id, conversation_id, title, command, cwd, str(log_path).replace("\\", "/"), now),
            )
        try:
            folder.mkdir(parents=True, exist_ok=True)
            argv = resolve_shell(self.app) + [command]
            kwargs: dict[str, Any] = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            else:
                kwargs["start_new_session"] = True
            with open(log_path, "ab") as fh:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=cwd,
                    env=env if env is not None else dict(os.environ),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=fh,
                    stderr=asyncio.subprocess.STDOUT,
                    **kwargs,
                )
        except (OSError, ValueError) as exc:
            return self._end(process_id, "failed", error=f"cannot start {command[:80]}: {exc}") or {}
        self._procs[process_id] = proc
        row = self._set(process_id, pid=proc.pid, status="running")
        assert row is not None
        self.app.bus.emit("process.started", row, conversation_id)
        self.app.spawn(f"process:{process_id}", self._watch(process_id, proc))
        return row

    async def stop(self, process_id: str, actor: str = "user") -> dict[str, Any] | None:
        """Kill the tree and mark the row `stopped`. A row that is not running comes back
        unchanged. The owner hears about it unless it did the stopping."""
        row = self.get(process_id)
        if row is None or row["status"] != "running":
            return row
        # Mark first: the awaiting watcher wakes after the kill and must see `stopped`,
        # not race it into `exited`.
        row = self._end(process_id, "stopped", error=f"stopped by {actor}")
        proc = self._procs.pop(process_id, None)
        pid = row.get("pid") if row else None
        await asyncio.to_thread(kill_tree, pid)
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except Exception:
                pass
        if row and actor != f"conversation:{row['conversation_id']}":
            self._post(row, "stopped", "stopped by the user")
        return self.get(process_id)

    # -- logs -----------------------------------------------------------------------
    def read_log(self, process_id: str, offset: int | None = None, limit: int | None = None) -> dict[str, Any] | None:
        """A character slice of the log. `limit` defaults to and is clamped by
        `conversation.tool_output_cap`; `offset=None` means the last `limit` chars."""
        row = self.get(process_id)
        if row is None:
            return None
        cap = int(self.app.settings.get("conversation.tool_output_cap"))
        limit = cap if limit is None else max(1, min(int(limit), cap))
        try:
            text = Path(row["log_path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        total = len(text)
        if offset is None:
            offset = max(0, total - limit)
        offset = max(0, int(offset))
        chunk = text[offset : offset + limit]
        return {"id": process_id, "offset": offset, "end": offset + len(chunk), "total": total, "text": chunk}

    def _tail(self, row: dict[str, Any]) -> str:
        try:
            text = Path(row["log_path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        text = text.strip()
        return text[-TAIL_CHARS:] if text else "(no output)"

    # -- watchers -----------------------------------------------------------------
    async def _watch(self, process_id: str, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()
        self._procs.pop(process_id, None)
        row = self.get(process_id)
        if row is None or row["status"] != "running":
            return  # stopped meanwhile; the stopper already said so
        row = self._end(process_id, "exited", exit_code=code)
        if row is not None:
            self._post(row, "exited", self._exit_text(row))

    async def _poll(self, process_id: str, pid: int) -> None:
        """Watcher for a process that outlived a backend restart: not our child any more,
        so probe the pid instead of awaiting it."""
        while True:
            await asyncio.sleep(self.poll_interval)
            if pid_alive(pid):
                continue
            row = self.get(process_id)
            if row is not None and row["status"] == "running":
                row = self._end(process_id, "exited", exit_code=None, error="exit code unknown: the backend restarted while it ran")
                if row is not None:
                    self._post(row, "exited", self._exit_text(row))
            return

    def _exit_text(self, row: dict[str, Any]) -> str:
        code = row.get("exit_code")
        code_text = "unknown" if code is None else str(code)
        return f"exited with code {code_text} after {duration(row)}. Last output:\n{self._tail(row)}"

    def _post(self, row: dict[str, Any], event: str, content: str) -> None:
        from .conversation import post  # lazy: conversation imports the tool packages

        try:
            post(
                self.app, row["conversation_id"], content,
                source=f"process:{row['id']}",
                meta={"title": row.get("title") or "", "event": event, "exit_code": row.get("exit_code")},
            )
        except Exception as exc:  # a vanished conversation must not take the watcher down
            log.warning("process %s: could not notify %s: %s", row["id"], row["conversation_id"], exc)

    # -- lifecycle --------------------------------------------------------------------
    async def reconcile(self) -> None:
        """Backend start: sort out what the last run left behind (module docstring)."""
        for row in self.list(status="starting"):
            self._end(row["id"], "failed", error="the backend restarted during spawn")
        for row in self.list(status="running"):
            pid = row.get("pid")
            if pid_alive(pid):
                self.app.spawn(f"process:{row['id']}", self._poll(row["id"], int(pid)))
            else:
                self._end(row["id"], "lost")

    async def shutdown(self) -> None:
        """Cancel the watchers; the children keep running (they own their log handles)."""
        for name, task in list(self.app.tasks.items()):
            if name.startswith("process:") and not task.done():
                task.cancel()
        self._procs.clear()

    def prune_archived(self, retention_days: int | None = None) -> int:
        """Rows of archived conversations past the artifact retention window; their log
        files already go with the artifact folder sweep. Running ones are left alone."""
        if retention_days is None:
            retention_days = int(self.app.settings.get("conversation.artifact_retention_days"))
        cutoff = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=int(retention_days))
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        rows = self.app.db.execute(
            "SELECT p.id FROM processes p JOIN conversations c ON c.id = p.conversation_id "
            "WHERE p.status != 'running' AND c.status='archived' AND c.archived_at IS NOT NULL AND c.archived_at < ?",
            (cutoff,),
        ).fetchall()
        for row in rows:
            self.app.db.execute("DELETE FROM processes WHERE id=?", (row["id"],))
        return len(rows)


# -- package hooks -------------------------------------------------------------------

def install(app: Any) -> None:
    app.processes = Processes(app)


async def start(app: Any) -> None:
    await app.processes.reconcile()


async def stop(app: Any) -> None:
    await app.processes.shutdown()
