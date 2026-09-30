"""Self-modification: the backend half of promotion (03 §15, charter §5.9).

A self-mod thread is an ordinary thread editing a git worktree — no framework lives
here for that. Code exists only for the three things a procedure cannot provide:
isolation (the worktree and scratch home, which the procedure note in the vault
describes), promotion with rollback, and protected paths.

Two tools, because the human must see *what* changes before the approval card appears
and a handler cannot run before the `ask` it is gated by (decided 2026-09-20, 03 §15.2):

    promote_plan(ref)   read-only: the commit, the changed files, which of them are
                        protected, whether the worktree is clean. Policy `allow`.
    promote(ref, acknowledge_protected?, skip_tests?)
                        the real thing. Policy `ask` (bootstrap row), so the dispatcher
                        has already put a human in the loop for the call itself; when
                        the diff touches protected paths the handler *additionally*
                        refuses until `acknowledge_protected` is set, naming the files.
                        `skip_tests` tells the supervisor to leave out the suite
                        (2026-09-29); the approval card shows it among the arguments.

`pull_checkout` is the first half of "Update from remote" (2026-09-29): it fast-forwards
the development checkout from its upstream, so a commit verified and pushed from another
machine can be promoted here without a terminal. The System page then plans and
promotes the new head through the ordinary path, with the suite skipped.

`request_promotion` is the one codepath for steps 1–3 — the tool and
`POST /api/system/promote` both call it, so a click and a model call cannot disagree.
Steps 4–10 belong to the supervisor, which also runs the test suite, inside the release
it built (2026-09-28: it used to run here, blocking the desk for the suite's duration
with nothing to show for it), and reports through `APP_HOME/promote.log`.
`read_progress` tails that file, republishes each line as `promote.progress`, and turns
a terminal line into a now-board item plus a message in the inbox of the conversation
that asked — a message, because that is what wakes it to report.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import bootstrap, conversation as conversations, items, prompts
from .conversation import events
from .tools import fs_guard
from .tools.registry import Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)

NOT_RUNNING = "supervisor not running — start Loom with `loom serve` (without --no-supervisor)"
PROGRESS_LOG = "promote.log"
PROGRESS_OFFSET = "promote.log.offset"
TERMINAL_STAGES = ("done", "failed")
HANDOFF_TIMEOUT_S = 180.0  # plan (with a fetch from origin) and the handoff; the suite runs in the supervisor
POLL_SECONDS = 2.0


# ---------------------------------------------------------------------------------
# git (subprocess; no GitPython — 03 §19)
# ---------------------------------------------------------------------------------
def _git(*args: str, cwd: Path | str, timeout: float = 120.0) -> tuple[bool, str]:
    """Run git and return (ok, output). A missing git is a failure like any other, and
    git never prompts: a fetch that needs credentials it does not have fails instead of
    waiting on a terminal nobody is watching."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env,
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
        lines.append("the supervisor runs the test suite inside the built release before the swap, "
                     "unless the promotion is asked to skip it")
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


def _fetch_from_origin(repo: Path, ref: str) -> bool:
    """A backend running from a release resolves refs in the supervisor's mirror
    (`releases/src.git`), which learns of new commits only when a release is built. A
    ref that lives in the mirror's origin — the development checkout — is promotable,
    so fetch it the way `create_release` will (2026-09-28: the second promotion, planned
    from a release, could not see a commit made in the checkout). False when there is
    no origin; the caller's retry decides whether the ref exists."""
    ok, _ = _git("remote", "get-url", "origin", cwd=repo)
    if not ok:
        return False
    _git("fetch", "--prune", "--tags", "origin", cwd=repo)
    _git("fetch", "origin", ref, cwd=repo)  # a bare sha, or a branch that is not a head here
    return True


def plan(app: Any, ref: str) -> Plan:
    """Resolve `ref`, diff it against the live commit, and mark the protected paths.
    Pure reading of the checkout: it never writes a file, never asks, never promotes
    (a ref the mirror lacks is fetched from its origin, which adds objects, not files)."""
    ref = (ref or "").strip()
    if not ref:
        return Plan(ref=ref, ok=False, code="bad_ref", error="ref is empty")
    repo = repo_root(app)
    if repo is None:
        return Plan(ref=ref, ok=False, code="no_repo",
                    error="this install has no source checkout, so there is nothing to promote")
    ok, out = _git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=repo)
    if not ok and _fetch_from_origin(repo, ref):
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


# ---------------------------------------------------------------------------------
# update from remote: fast-forward the development checkout (2026-09-29)
# ---------------------------------------------------------------------------------
def checkout_root(app: Any) -> Path | None:
    """The development checkout: the repo the backend runs from, or, when that is a
    release, the checkout it was built from. A release's origin is a local path (the
    supervisor's mirror clones the checkout), the checkout's origin is the real remote,
    so follow local-directory origins until one is not."""
    repo = repo_root(app)
    for _ in range(3):  # release worktree -> checkout; one spare hop
        if repo is None:
            return None
        ok, url = _git("remote", "get-url", "origin", cwd=repo)
        target = Path(url.strip()) if ok and url.strip() else None
        if target is None or not target.is_dir() or not (target / ".git").exists():
            return repo
        repo = target
    return repo


def pull_checkout(app: Any) -> dict[str, Any]:
    """Fetch the checkout's upstream and fast-forward its branch. Returns
    `{ok, code, message, checkout, branch, upstream, before, after, commits, live_commit}`;
    `code` is `ok|no_repo|detached|no_upstream|dirty|fetch_failed|diverged`. It never
    merges, rebases or stashes: anything but a clean fast-forward is refused with the
    reason, because the checkout is the person's working tree."""
    root = checkout_root(app)
    live = repo_root(app)
    _, head = _git("rev-parse", "HEAD", cwd=live) if live else (False, "")
    out: dict[str, Any] = {"ok": False, "code": "no_repo", "message": "", "checkout": "",
                           "branch": "", "upstream": "", "before": "", "after": "",
                           "commits": [], "live_commit": head.strip().splitlines()[-1] if head.strip() else ""}
    if root is None:
        out["message"] = "this install has no source checkout, so there is nothing to update"
        return out
    out["checkout"] = str(root).replace("\\", "/")

    def refuse(code: str, message: str) -> dict[str, Any]:
        out.update(code=code, message=message)
        return out

    ok, branch = _git("symbolic-ref", "--short", "-q", "HEAD", cwd=root)
    if not ok or not branch.strip():
        return refuse("detached", f"the checkout at {out['checkout']} is not on a branch")
    out["branch"] = branch.strip()
    ok, upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", cwd=root)
    if not ok or not upstream.strip():
        return refuse("no_upstream", f"branch {out['branch']} has no upstream to pull from")
    out["upstream"] = upstream.strip().splitlines()[-1]
    ok, status = _git("status", "--porcelain", cwd=root)
    dirty = [line.strip().split(None, 1)[-1] for line in status.splitlines() if line.strip()] if ok else []
    if dirty:
        return refuse("dirty", f"the checkout at {out['checkout']} has uncommitted changes "
                               f"({', '.join(dirty[:10])}); commit or stash them first")
    remote = out["upstream"].split("/", 1)[0]
    ok, fetched = _git("fetch", "--prune", remote, cwd=root)
    if not ok:
        return refuse("fetch_failed", f"could not fetch {remote}: {fetched[-500:]}")
    _, before = _git("rev-parse", "HEAD", cwd=root)
    out["before"] = before.strip()
    ok, counts = _git("rev-list", "--left-right", "--count", "HEAD...@{u}", cwd=root)
    ahead, behind = (int(n) for n in counts.split()) if ok and len(counts.split()) == 2 else (0, 0)
    if ahead and behind:
        return refuse("diverged", f"{out['branch']} and {out['upstream']} have diverged "
                                  f"({ahead} local, {behind} remote commit(s)); reconcile them in a terminal")
    if behind:
        ok, merged = _git("merge", "--ff-only", "@{u}", cwd=root)
        if not ok:
            return refuse("diverged", f"could not fast-forward {out['branch']}: {merged[-500:]}")
    _, after = _git("rev-parse", "HEAD", cwd=root)
    out["after"] = after.strip()
    if out["after"] != out["before"]:
        _, log_ = _git("log", "--oneline", f"{out['before']}..{out['after']}", cwd=root)
        out["commits"] = [line for line in log_.splitlines() if line.strip()]
    pulled = len(out["commits"])
    state = f"pulled {pulled} commit(s)" if pulled else "already up to date with the remote"
    live_note = "; that is the live commit" if out["after"] == out["live_commit"] else ""
    out.update(ok=True, code="ok",
               message=f"{out['branch']} {state} at {out['after'][:7]}{live_note}")
    return out


def _is_protected(app: Any, repo: Path, relative: str) -> bool:
    """A protected-paths row may be stored repo-relative or absolute; ask about both so
    neither spelling can slip a change past the guard (03 §15.3)."""
    return fs_guard.is_protected(app, str(repo / relative)) or fs_guard.is_protected(app, relative)


# ---------------------------------------------------------------------------------
# steps 1–3, the one codepath (tool and REST both call this)
# ---------------------------------------------------------------------------------
async def request_promotion(
    app: Any,
    ref: str,
    conversation_id: str | None = None,
    acknowledge_protected: bool = False,
    skip_tests: bool = False,
) -> dict[str, Any]:
    """Check and hand the ref to the supervisor. Returns
    `{ok, code, message, plan, output?}`; `code` is the failure class (03 §17), so
    every caller can map one behaviour onto it. `skip_tests` is passed through: the
    supervisor logs a skipped `tests` stage instead of running the suite."""
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

    client = app.supervisor or SupervisorClient(app)
    answer = await client.promote(plan_.ref, conversation_id or "", skip_tests=skip_tests)
    if not answer.get("ok"):
        payload["code"] = "no_supervisor" if answer.get("error") == NOT_RUNNING else "supervisor_error"
        payload["message"] = str(answer.get("error") or "the supervisor refused the promotion")
        return payload

    # The next promotion is starting: the previous outcome (success or failure) is
    # superseded, so its item leaves the now board.
    for item in items.by_source(app, "promote"):
        if item["dismissed_at"] is None:
            items.dismiss(app, item["id"])

    skipped = " with the test suite skipped" if skip_tests else ""
    if conversation_id:
        _system_event(
            app, conversation_id,
            f"promotion of {plan_.short} handed to the supervisor{skipped}; this turn ends "
            "here, and the outcome arrives as a message after the restart",
        )
    return {"ok": True, "code": "ok", "plan": plan_.to_json(), "skip_tests": skip_tests,
            "message": f"promotion of {plan_.short} ({plan_.ref}) handed to the supervisor{skipped}"}


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

    async def promote(self, ref: str, conversation_id: str = "", skip_tests: bool = False) -> dict[str, Any]:
        return await self._call("POST", "/promote", {
            "ref": ref, "conversation_id": conversation_id, "skip_tests": skip_tests,
        })

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
    `promote.progress`, and turn a terminal line into an item and, when a conversation
    asked for the promotion, a message in its inbox — which wakes it to report, since
    the turn that asked ended at the handoff (2026-09-28: a `system` event sat unread
    while the desk had promised to report). Runs on the event loop for that reason. The
    offset is a file, so a restart in the middle of a promotion reports it exactly once."""
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
            try:
                conversations.post(app, conversation_id, f"{title.lower()}: {body}",
                                   source="promote", meta={"ref": ref, "ok": ok})
            except Exception:  # the tail must never break on one conversation
                log.exception("could not post the promotion outcome to %s", conversation_id)
    _set_offset(app, end)
    return entries


async def _tail(app: Any) -> None:
    while not app.shutting_down:
        try:
            read_progress(app)  # on the loop: the post wakes the conversation
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
        skip_tests=args.get("skip_tests") is True,
    )
    if not outcome["ok"]:
        content = str(outcome["message"])
        if outcome.get("output"):
            content += "\n\n" + str(outcome["output"])
        return ToolResult.error(content, code=outcome["code"], ref=ref)
    return ToolResult(
        content=str(outcome["message"]) + "; this turn ends here, and the outcome arrives as a message after the restart",
        meta={"ends_turn": True, "ref": ref, "commit": outcome["plan"]["commit"],
              "skip_tests": outcome["skip_tests"]},
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
                    "skip_tests": {
                        "type": "boolean",
                        "default": False,
                        "description": "Leave out the supervisor's test suite. Only when the "
                                       "person has told you to skip it for this promotion.",
                    },
                },
                "required": ["ref"],
            },
            handler=promote_handler,
            destructive=True,
            timeout_s=HANDOFF_TIMEOUT_S,
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
        read_progress(app)  # on the loop: the post wakes the conversation
    except Exception:
        log.exception("could not read the promote log")
    app.spawn("promote.tail", _tail(app))


async def stop(app: Any) -> None:
    task = app.tasks.pop("promote.tail", None)
    if task is not None and not task.done():
        task.cancel()
