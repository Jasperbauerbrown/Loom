"""Self-modification: the backend half of promotion (03 §15, charter §5.9).

A self-mod thread is an ordinary thread editing a git worktree — no framework lives
here for that. Code exists only for the three things a procedure cannot provide:
isolation (the worktree and scratch home, which the procedure note in the vault
describes), promotion with rollback, and protected paths.

Two tools, because the human must see *what* changes before the approval card appears
and a handler cannot run before the `ask` it is gated by (decided 2026-09-20, 03 §15.2):

    promote_plan(ref)   read-only: the commit, the changed files, which of them are
                        protected, whether the worktree is clean. Policy `allow`.
    promote(ref, acknowledge_protected?)
                        the real thing. Policy `ask` (bootstrap row), so the dispatcher
                        has already put a human in the loop for the call itself; when
                        the diff touches protected paths the handler *additionally*
                        refuses until `acknowledge_protected` is set, naming the files.

`request_promotion` is the one codepath for steps 1–4 — the tool and
`POST /api/system/promote` both call it, so a click and a model call cannot disagree.
Steps 5–10 belong to the supervisor, which reports through `APP_HOME/promote.log`;
`read_progress` tails that file, republishes each line as `promote.progress`, and turns
a terminal line into a now-board item plus a `system` event in the thread that asked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import bootstrap, items, prompts
from .conversation import events
from .tools import fs_guard
from .tools.registry import Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)

NOT_RUNNING = "supervisor not running — start Loom with `loom serve` (without --no-supervisor)"
PROGRESS_LOG = "promote.log"
PROGRESS_OFFSET = "promote.log.offset"
TERMINAL_STAGES = ("done", "failed")
TEST_TIMEOUT_S = 600.0
POLL_SECONDS = 2.0


# ---------------------------------------------------------------------------------
# git (subprocess; no GitPython — 03 §19)
# ---------------------------------------------------------------------------------
def _git(*args: str, cwd: Path | str, timeout: float = 120.0) -> tuple[bool, str]:
    """Run git and return (ok, output). A missing git is a failure like any other."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"git: {exc}"
    return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()


# ---------------------------------------------------------------------------------
# the plan (03 §15.2 steps 1–2, read-only)
# ---------------------------------------------------------------------------------
@dataclass
class Plan:
    ref: str
    ok: bool = True
    error: str = ""
    code: str = "ok"
    commit: str = ""
    short: str = ""
    current_commit: str = ""
    changed_files: list[str] = field(default_factory=list)
    protected_files: list[str] = field(default_factory=list)
    dirty_files: list[str] = field(default_factory=list)
    clean: bool = True
    worktree: str = ""
    tests_required: bool = True
    repo: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        if not self.ok:
            return f"error: {self.error}"
        lines = [
            f"ref {self.ref} → {self.short} ({self.commit})",
            f"live commit {self.current_commit[:7] or '(unknown)'}",
            f"{len(self.changed_files)} file(s) change; worktree {'clean' if self.clean else 'DIRTY'}",
        ]
        if self.dirty_files:
            lines.append("uncommitted: " + ", ".join(self.dirty_files[:20]))
        if self.protected_files:
            lines.append("PROTECTED paths in this diff (promotion needs acknowledge_protected=true "
                         "and a human approval): " + ", ".join(self.protected_files))
        else:
            lines.append("no protected paths in this diff")
        lines.append("the test suite runs in the ref's worktree before anything is promoted")
        if self.changed_files:
            shown = self.changed_files[:50]
            lines.append("changed:\n  " + "\n  ".join(shown))
            if len(self.changed_files) > len(shown):
                lines.append(f"  … {len(self.changed_files) - len(shown)} more")
        return "\n".join(lines)


def repo_root(app: Any) -> Path | None:
    """The checkout the backend runs from; None when installed without its repo."""
    return bootstrap.repo_root()


def _worktree_for(repo: Path, ref: str, commit: str) -> str:
    """The worktree that has `ref` checked out, if any — that is the one whose
    cleanliness matters, because it is the tree the commit came from."""
    ok, out = _git("worktree", "list", "--porcelain", cwd=repo)
    if not ok:
        return ""
    path, head, branch = "", "", ""
    found = ""
    for line in out.splitlines() + [""]:
        if line.startswith("worktree "):
            path, head, branch = line[len("worktree "):].strip(), "", ""
        elif line.startswith("HEAD "):
            head = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            branch = line[len("branch "):].strip()
        elif not line.strip() and path:
            if head == commit or branch in (f"refs/heads/{ref}", ref):
                found = found or path
            path = ""
    return found


def plan(app: Any, ref: str) -> Plan:
    """Resolve `ref`, diff it against the live commit, and mark the protected paths.
    Pure reading: it never writes, never asks, never promotes."""
    ref = (ref or "").strip()
    if not ref:
        return Plan(ref=ref, ok=False, code="bad_ref", error="ref is empty")
    repo = repo_root(app)
    if repo is None:
        return Plan(ref=ref, ok=False, code="no_repo",
                    error="this install has no source checkout, so there is nothing to promote")
    ok, out = _git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=repo)
    if not ok:
        return Plan(ref=ref, ok=False, code="bad_ref", repo=str(repo),
                    error=f"{ref} does not resolve to a commit in {repo}: {out}")
    commit = out.splitlines()[-1].strip()
    _, head = _git("rev-parse", "HEAD", cwd=repo)
    current = head.splitlines()[-1].strip() if head else ""

    changed: list[str] = []
    if current and current != commit:
        ok, out = _git("diff", "--name-only", current, commit, cwd=repo)
        if not ok:
            return Plan(ref=ref, ok=False, code="bad_ref", repo=str(repo),
                        error=f"could not diff {current[:7]}..{commit[:7]}: {out}")
        changed = [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]

    protected = [p for p in changed if _is_protected(app, repo, p)]

    worktree = _worktree_for(repo, ref, commit)
    dirty: list[str] = []
    if worktree:
        ok, out = _git("status", "--porcelain", cwd=worktree)
        if ok:
            # `--porcelain` is "XY path"; the leading status columns may already have
            # been stripped off the first line, so split on whitespace, not columns.
            dirty = [line.strip().split(None, 1)[-1].strip() for line in out.splitlines() if line.strip()]

    return Plan(
        ref=ref, commit=commit, short=commit[:7], current_commit=current,
        changed_files=changed, protected_files=protected, dirty_files=dirty,
        clean=not dirty, worktree=worktree.replace("\\", "/"), repo=str(repo).replace("\\", "/"),
    )


def _is_protected(app: Any, repo: Path, relative: str) -> bool:
    """A protected-paths row may be stored repo-relative or absolute; ask about both so
    neither spelling can slip a change past the guard (03 §15.3)."""
    return fs_guard.is_protected(app, str(repo / relative)) or fs_guard.is_protected(app, relative)


# ---------------------------------------------------------------------------------
# the test suite (03 §15.2 step 3)
# ---------------------------------------------------------------------------------
async def run_tests(worktree: Path | str, timeout_s: float = TEST_TIMEOUT_S) -> tuple[bool, str]:
    """`python -m uv run pytest -q -x` in the ref's worktree. Returns (ok, output);
    a timeout or a missing uv is a failure with its reason as the output."""
    command = [sys.executable, "-m", "uv", "run", "pytest", "-q", "-x"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, cwd=str(worktree),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return False, f"could not run the test suite: {exc}"
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False, f"the test suite did not finish within {timeout_s:g}s"
    output = (raw or b"").decode("utf-8", errors="replace")
    return proc.returncode == 0, output


async def _tests_for(app: Any, plan_: Plan) -> tuple[bool, str]:
    """Run the suite where `ref` lives; when it lives nowhere, in a throwaway worktree
    under `releases/test-<sha7>` that is removed again either way."""
    if plan_.worktree:
        return await run_tests(plan_.worktree)
    repo = Path(plan_.repo)
    temp = Path(app.releases_dir) / f"test-{plan_.short}"
    temp.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(temp, ignore_errors=True)
    ok, out = await asyncio.to_thread(
        _git, "worktree", "add", "--detach", str(temp), plan_.commit, cwd=repo, timeout=600.0
    )
    if not ok:
        return False, f"could not create a worktree for {plan_.short}: {out}"
    try:
        return await run_tests(temp)
    finally:
        await asyncio.to_thread(_git, "worktree", "remove", "--force", str(temp), cwd=repo, timeout=300.0)
        shutil.rmtree(temp, ignore_errors=True)


# ---------------------------------------------------------------------------------
# steps 1–4, the one codepath (tool and REST both call this)
# ---------------------------------------------------------------------------------
async def request_promotion(
    app: Any,
    ref: str,
    conversation_id: str | None = None,
    acknowledge_protected: bool = False,
    run_suite: bool = True,
) -> dict[str, Any]:
    """Check, test, and hand the ref to the supervisor. Returns
    `{ok, code, message, plan, output?}`; `code` is the failure class (03 §17), so
    every caller can map one behaviour onto it."""
    plan_ = await asyncio.to_thread(plan, app, ref)
    payload: dict[str, Any] = {"ok": False, "code": plan_.code, "message": plan_.error,
                               "plan": plan_.to_json()}
    if not plan_.ok:
        return payload

    if not plan_.clean:
        payload["code"] = "dirty"
        payload["message"] = (
            f"the worktree at {plan_.worktree} has uncommitted changes "
            f"({', '.join(plan_.dirty_files[:10])}); commit or stash them, then promote the commit"
        )
        return payload

    if plan_.protected_files and not acknowledge_protected:
        payload["code"] = "protected"
        payload["message"] = (
            "this commit changes protected paths: " + ", ".join(plan_.protected_files)
            + " — the person must confirm on the System page, or you may call "
            "promote(ref, acknowledge_protected=true), which still needs their approval"
        )
        return payload

    if run_suite:
        ok, output = await _tests_for(app, plan_)
        if not ok:
            payload["code"] = "tests_failed"
            payload["message"] = f"the test suite fails at {plan_.short}; nothing was promoted"
            payload["output"] = output
            return payload

    client = app.supervisor or SupervisorClient(app)
    answer = await client.promote(plan_.ref, conversation_id or "")
    if not answer.get("ok"):
        payload["code"] = "no_supervisor" if answer.get("error") == NOT_RUNNING else "supervisor_error"
        payload["message"] = str(answer.get("error") or "the supervisor refused the promotion")
        return payload

    if conversation_id:
        _system_event(
            app, conversation_id,
            f"promotion of {plan_.short} started; this turn ends here and the thread "
            "resumes after the restart, with the outcome in the promote log",
        )
    return {"ok": True, "code": "ok", "plan": plan_.to_json(),
            "message": f"promotion of {plan_.short} ({plan_.ref}) handed to the supervisor"}


def _system_event(app: Any, conversation_id: str, text: str) -> None:
    event_id = events.append(app, conversation_id, "system", text)
    app.bus.emit("event.appended", events.get(app, event_id), conversation_id)


# ---------------------------------------------------------------------------------
# the supervisor's loopback API (`app.supervisor`)
# ---------------------------------------------------------------------------------
class SupervisorClient:
    """`127.0.0.1:<port+1>` with the token from `APP_HOME/supervisor.token`. Every
    method answers `{"ok": bool, ...}`; a supervisor that is not running is not an
    exception, it is a `{"ok": false, "error": …}` the UI and the model can read."""

    def __init__(self, app: Any):
        self.app = app

    @property
    def port(self) -> int:
        return int(self.app.settings.get("general.port") or 8500) + 1

    @property
    def token(self) -> str:
        path = Path(self.app.home) / "supervisor.token"
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @property
    def running(self) -> bool:
        return bool(self.token)

    async def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self.token
        if not token:
            return {"ok": False, "error": NOT_RUNNING}
        import httpx

        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                response = await http.request(
                    method, url, json=payload or {}, headers={"X-Supervisor-Token": token}
                )
        except httpx.HTTPError:
            return {"ok": False, "error": NOT_RUNNING}
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str(response.json().get("detail") or "")
            except ValueError:
                detail = response.text[:200]
            return {"ok": False, "error": f"supervisor refused {path}: {detail or response.status_code}"}
        try:
            body = response.json()
        except ValueError:
            body = {}
        return {"ok": True, **(body if isinstance(body, dict) else {"result": body})}

    async def status(self) -> dict[str, Any]:
        return await self._call("GET", "/status")

    async def promote(self, ref: str, conversation_id: str = "") -> dict[str, Any]:
        return await self._call("POST", "/promote", {"ref": ref, "conversation_id": conversation_id})

    async def rollback(self, conversation_id: str = "") -> dict[str, Any]:
        return await self._call("POST", "/rollback", {"conversation_id": conversation_id})

    async def restart(self) -> dict[str, Any]:
        return await self._call("POST", "/restart", {})


# ---------------------------------------------------------------------------------
# the progress log (03 §15.2 step 10, §17 "promotion failure → rollback + item")
# ---------------------------------------------------------------------------------
def log_path(app: Any) -> Path:
    return Path(app.home) / PROGRESS_LOG


def read_log(app: Any, limit: int = 200) -> list[dict[str, Any]]:
    """The whole log, newest last, for the System screen."""
    return _parse(_read_text(log_path(app)))[-limit:]


def _read_text(path: Path, offset: int = 0) -> tuple[str, int]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            if offset > size:  # the log was truncated or replaced
                offset = 0
            handle.seek(offset)
            raw = handle.read()
        return raw.decode("utf-8", errors="replace"), offset + len(raw)
    except OSError:
        return "", offset


def _parse(text_and_offset: tuple[str, int] | str) -> list[dict[str, Any]]:
    text = text_and_offset[0] if isinstance(text_and_offset, tuple) else text_and_offset
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _offset(app: Any) -> int:
    path = Path(app.home) / PROGRESS_OFFSET
    try:
        return int(path.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def _set_offset(app: Any, value: int) -> None:
    try:
        (Path(app.home) / PROGRESS_OFFSET).write_text(str(value), encoding="utf-8")
    except OSError:
        pass


def read_progress(app: Any) -> list[dict[str, Any]]:
    """Consume what the supervisor appended since last time: republish every line as
    `promote.progress`, and turn a terminal line into an item and, when a thread asked
    for the promotion, a `system` event it will read when it resumes. The offset is a
    file, so a restart in the middle of a promotion reports it exactly once."""
    start = _offset(app)
    text, end = _read_text(log_path(app), start)
    entries = _parse(text)
    if not entries:
        if end != start:
            _set_offset(app, end)
        return []
    for entry in entries:
        conversation_id = str(entry.get("conversation_id") or "") or None
        app.bus.emit(
            "promote.progress",
            {
                "stage": entry.get("stage", ""),
                "ok": bool(entry.get("ok")),
                "message": entry.get("message", ""),
                "ref": entry.get("ref", ""),
                "ts": entry.get("ts", ""),
            },
            conversation_id,
        )
        if str(entry.get("stage")) not in TERMINAL_STAGES:
            continue
        ok = bool(entry.get("ok"))
        ref = str(entry.get("ref") or "")
        title = f"Promotion of {ref or 'the release'} {'succeeded' if ok else 'failed'}"
        body = str(entry.get("message") or "")
        try:
            items.post(app, "notification", title, body, conversation_id=conversation_id,
                       source="promote", notify=not ok)
        except Exception:  # an item must never break the tail
            log.exception("could not post the promotion item")
        if conversation_id and app.conversation(conversation_id) is not None:
            _system_event(app, conversation_id, f"{title.lower()}: {body}")
    _set_offset(app, end)
    return entries


async def _tail(app: Any) -> None:
    while not app.shutting_down:
        try:
            await asyncio.to_thread(read_progress, app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("promote log tail failed")
        await asyncio.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------------
async def promote_plan_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    plan_ = await asyncio.to_thread(plan, ctx.app, str(args.get("ref") or ""))
    if not plan_.ok:
        return ToolResult.error(plan_.error, code=plan_.code)
    return ToolResult(content=plan_.summary(), meta={
        "ref": plan_.ref, "commit": plan_.commit,
        "protected": plan_.protected_files, "clean": plan_.clean,
    })


async def promote_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ref = str(args.get("ref") or "").strip()
    if not ref:
        return ToolResult.error("ref is empty")
    outcome = await request_promotion(
        ctx.app, ref, ctx.conversation_id,
        acknowledge_protected=bool(args.get("acknowledge_protected")),
    )
    if not outcome["ok"]:
        content = str(outcome["message"])
        if outcome.get("output"):
            content += "\n\n" + str(outcome["output"])
        return ToolResult.error(content, code=outcome["code"], ref=ref)
    return ToolResult(
        content=str(outcome["message"]) + "; this turn ends here and resumes after the restart",
        meta={"ends_turn": True, "ref": ref, "commit": outcome["plan"]["commit"]},
    )


def tools() -> list[Tool]:
    return [
        Tool(
            name="promote_plan",
            description=prompts.load("tools/promote_plan"),
            parameters={
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Commit, tag or branch in this repo."},
                },
                "required": ["ref"],
            },
            handler=promote_plan_handler,
            read_only=True,
        ),
        Tool(
            name="promote",
            description=prompts.load("tools/promote"),
            parameters={
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Commit, tag or branch to bring live."},
                    "acknowledge_protected": {
                        "type": "boolean",
                        "default": False,
                        "description": "Set only after promote_plan listed protected paths and you "
                                       "are reporting them to the person; the call still needs "
                                       "their approval.",
                    },
                },
                "required": ["ref"],
            },
            handler=promote_handler,
            destructive=True,
            timeout_s=TEST_TIMEOUT_S + 60.0,
        ),
    ]


# ---------------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------------
# The workflow lives in the vault, not in Python (charter §5.9): a procedure note the
# assistant loads like any other, ensured with the other built-in procedures by
# `knowledge.install` (`vault.BUILTIN_PROCEDURES`).
PROCEDURE_PATH = "procedures/self-modification/SKILL.md"


def install(app: Any) -> None:
    app.supervisor = SupervisorClient(app)
    for tool in tools():
        app.tools.register(tool, replace=True)


async def start(app: Any) -> None:
    """Report anything the supervisor logged while this backend was not running (a
    promotion ends with a restart, so the last stages always land here), then keep
    tailing."""
    try:
        await asyncio.to_thread(read_progress, app)
    except Exception:
        log.exception("could not read the promote log")
    app.spawn("promote.tail", _tail(app))


async def stop(app: Any) -> None:
    task = app.tasks.pop("promote.tail", None)
    if task is not None and not task.done():
        task.cancel()
