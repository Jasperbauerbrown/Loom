"""Worktree isolation for a thread (03 §6.7, generalising §15.1).

A thread that will change a git repository can get its own checkout: `conversation_create
(isolation="worktree")` runs `git worktree add -b loom/<id> APP_HOME/worktrees/<id> HEAD`
in the repository that holds its source root, and that checkout *replaces* the source
root in the thread's roots. Its work is then isolated from the live tree and from every
sibling thread by construction: nothing needs refusing, because the live checkout is not
among the paths the thread may touch. The branch is how the work comes back — archiving
releases the checkout, never the branch.

Git is driven through the CLI (03 §19: no GitPython), exactly as `knowledge/git.py` does
it. All of it is synchronous subprocess work: creation happens once per thread and
removal once per archive, so neither is on a hot path and neither needs a thread hop.

Failure is an expected input with one defined behaviour per class:
- not a repository / no commit / the path exists / git refuses → `WorktreeError`, whose
  message is a sentence the model or the person can act on (git's own words included);
- a checkout with uncommitted changes, or one git will not delete (Windows locks a file,
  a process still sits in it) → `remove` keeps it and says why. Nothing is ever deleted
  because it was easier than explaining.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from . import events

log = logging.getLogger(__name__)

BRANCH_PREFIX = "loom/"
DIRNAME = "worktrees"


class WorktreeError(Exception):
    pass


# -- plumbing ---------------------------------------------------------------------

def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "loom")
    env.setdefault("GIT_AUTHOR_EMAIL", "loom@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "loom")
    env.setdefault("GIT_COMMITTER_EMAIL", "loom@localhost")
    env["GIT_TERMINAL_PROMPT"] = "0"  # never block a turn on a credential prompt
    return env


def _git(args: list[str], cwd: str) -> str:
    """One git command. Returns stdout; raises `WorktreeError` carrying git's own
    message, which is the only thing that explains a refusal."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise WorktreeError("git is not installed or not on PATH") from exc
    except OSError as exc:
        raise WorktreeError(f"git could not run in {cwd}: {exc}") from exc
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip()
        raise WorktreeError(message or f"git {' '.join(args)} failed ({proc.returncode})")
    return proc.stdout


def same_path(a: str, b: str) -> bool:
    """Windows paths compare case-insensitively, POSIX exactly — the rule `fs_guard`
    already applies, kept here so callers need not reach into it."""
    if os.name == "nt":
        return a.replace("\\", "/").lower() == b.replace("\\", "/").lower()
    return a.replace("\\", "/") == b.replace("\\", "/")


# -- questions --------------------------------------------------------------------

def repo_root(path: str) -> str | None:
    """The top level of the git repository containing `path` (forward slashes), or None
    when it is not in one. A path that does not exist is asked about from its parent, so
    a root the thread means to create still finds its repository."""
    target = str(path or "").strip().replace("\\", "/")
    if not target:
        return None
    while target and not os.path.isdir(target):
        parent = os.path.dirname(target)
        if parent == target:
            return None
        target = parent
    if not target:
        return None
    try:
        out = _git(["rev-parse", "--show-toplevel"], cwd=target)
    except WorktreeError:
        return None
    root = out.strip().replace("\\", "/")
    return root or None


def branch_for(conversation_id: str) -> str:
    return f"{BRANCH_PREFIX}{conversation_id}"


def path_for(app, conversation_id: str) -> str:  # type: ignore[no-untyped-def]
    """`APP_HOME/worktrees/<conversation_id>` — beside `releases/` and `artifacts/`, so
    every checkout Loom owns is in one place and none of them is inside a user repo."""
    return str(Path(app.home) / DIRNAME / conversation_id).replace("\\", "/")


# -- create / remove ---------------------------------------------------------------

def create(app, conversation_id: str, source_root: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """`git worktree add -b loom/<id> APP_HOME/worktrees/<id> HEAD` in the repository
    that holds `source_root`. Returns `{"path", "branch", "repo"}`."""
    repo = repo_root(source_root)
    if repo is None:
        raise WorktreeError(
            f"{source_root} is not inside a git repository; worktree isolation needs one — "
            "make it a repository with one commit, or spawn the thread without isolation"
        )
    try:
        _git(["rev-parse", "--verify", "HEAD"], cwd=repo)
    except WorktreeError as exc:
        raise WorktreeError(
            f"the git repository at {repo} has no commit yet; a worktree branches from HEAD, "
            "so make one commit first"
        ) from exc
    path = path_for(app, conversation_id)
    branch = branch_for(conversation_id)
    if os.path.exists(path):
        raise WorktreeError(f"{path} already exists; remove it before a worktree can be created there")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        _git(["worktree", "add", "-b", branch, path, "HEAD"], cwd=repo)
    except WorktreeError as exc:
        raise WorktreeError(f"git worktree add failed in {repo}: {exc}") from exc
    log.info("worktree %s created for %s from %s", path, conversation_id, repo)
    return {"path": path, "branch": branch, "repo": repo}


def main_repo(path: str) -> str | None:
    """The repository a linked worktree belongs to, so `git worktree remove` runs from
    the main checkout. Running it with the worktree as the process's own directory is
    what Windows refuses (a directory that is a running process's cwd cannot be
    deleted)."""
    try:
        common = _git(["rev-parse", "--git-common-dir"], cwd=path).strip().replace("\\", "/")
    except WorktreeError:
        return None
    if not common:
        return None
    if not os.path.isabs(common):
        common = os.path.normpath(os.path.join(path, common)).replace("\\", "/")
    root = os.path.dirname(common) if os.path.basename(common) == ".git" else common
    return root.replace("\\", "/") or None


def remove(app, conv: dict[str, Any]) -> tuple[bool, str]:  # type: ignore[no-untyped-def]
    """Release a thread's worktree. `git worktree remove` when the checkout is clean,
    otherwise leave it in place — uncommitted work is the person's, not ours. Returns
    `(removed, one-line message naming the path and, when kept, why)`."""
    path = str(conv.get("worktree") or "").strip()
    if not path:
        return False, "this conversation has no worktree"
    branch = branch_for(str(conv.get("id") or ""))
    if not os.path.isdir(path):
        return True, f"worktree {path} was already gone; branch {branch} is untouched"
    try:
        dirty = _git(["status", "--porcelain", "-uall"], cwd=path).strip()
    except WorktreeError as exc:
        return False, f"worktree {path} kept: {exc}"
    if dirty:
        n = len(dirty.splitlines())
        return False, f"worktree {path} kept: {n} uncommitted change(s) on branch {branch}"
    repo = main_repo(path)
    if repo is None:
        return False, f"worktree {path} kept: its main repository could not be found"
    try:
        _git(["worktree", "remove", path], cwd=repo)
    except WorktreeError as exc:
        return False, f"worktree {path} kept: {exc}"
    return True, f"worktree {path} removed; branch {branch} keeps the work"


def release(app, conv: dict[str, Any] | None) -> str:  # type: ignore[no-untyped-def]
    """Archiving releases the thread's checkout: `remove` it, clear the column only when
    it really went, and record the outcome as a `system` event in the thread's own
    transcript either way. The one codepath for both archive routes — the person's
    button (`POST /conversations/{id}/archive`) and maintenance's nightly housekeeping —
    so a scheduled archive and a clicked one cannot disagree. Returns the message, or ""
    when there was no worktree. Never raises: the archive has already happened."""
    if not conv or not conv.get("worktree"):
        return ""
    try:
        removed, message = remove(app, conv)
    except Exception as exc:  # a broken git must never turn archiving into an error
        log.warning("releasing the worktree of %s failed: %r", conv.get("id"), exc)
        removed, message = False, f"worktree {conv['worktree']} kept: {type(exc).__name__}: {exc}"
    if removed:
        events.update(app, conv["id"], worktree=None)
    events.append(app, conv["id"], "system", message)
    events.announce(app, conv["id"])
    return message
