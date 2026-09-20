"""The vault's git repository, driven through the git CLI (03 §19: no GitPython).

Every vault mutation commits, so the *Recent changes* view (03 §13.1) is `git log` and
revert is `git revert` / `git checkout` — history the user can also read in any git
client or in Obsidian's own tooling.

Failure is an expected input: git is not a hard dependency of correctness. If a command
fails (no git on PATH, a locked index, a repository the user manipulated by hand) the
error is logged and the caller gets `None`; the markdown on disk is still the truth.

Sync methods are used from synchronous writers; `a_*` wrappers run the same call in a
thread for async callers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_IDENTITY = ("loom", "loom@localhost")
_SEP = "\x1f"


class GitError(Exception):
    pass


class Git:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.available = True

    # -- plumbing ---------------------------------------------------------------
    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        name, email = DEFAULT_IDENTITY
        env.setdefault("GIT_AUTHOR_NAME", name)
        env.setdefault("GIT_AUTHOR_EMAIL", email)
        env.setdefault("GIT_COMMITTER_NAME", name)
        env.setdefault("GIT_COMMITTER_EMAIL", email)
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def run(self, *args: str, check: bool = True) -> str:
        """Run one git command in the vault. Returns stdout; raises `GitError` when
        `check` and the command failed."""
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(self.root),
                env=self._env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:  # git missing: degrade, never crash the vault
            self.available = False
            if check:
                raise GitError("git is not installed or not on PATH") from exc
            return ""
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "").strip()
            if check:
                raise GitError(f"git {' '.join(args)}: {message}")
            return ""
        return proc.stdout

    def _returncode(self, *args: str) -> int:
        """Exit status only, for probe commands like `diff --cached --quiet`."""
        try:
            proc = subprocess.run(
                ["git", *args], cwd=str(self.root), env=self._env(), capture_output=True, text=True
            )
        except FileNotFoundError:
            self.available = False
            return 127
        return proc.returncode

    def _try(self, *args: str) -> str | None:
        try:
            return self.run(*args)
        except GitError as exc:
            log.warning("vault git: %s", exc)
            return None

    # -- repository -------------------------------------------------------------
    @property
    def initialised(self) -> bool:
        return (self.root / ".git").exists()

    def init(self) -> bool:
        """`git init` when the vault is not yet a repository. Returns True if it ran."""
        if self.initialised:
            return False
        self.root.mkdir(parents=True, exist_ok=True)
        self.run("init")
        self.run("config", "user.name", DEFAULT_IDENTITY[0])
        self.run("config", "user.email", DEFAULT_IDENTITY[1])
        return True

    def head(self) -> str | None:
        out = self._try("rev-parse", "HEAD")
        return out.strip() if out else None

    # -- writing ----------------------------------------------------------------
    def commit(self, message: str, paths: list[str] | None = None) -> str | None:
        """Stage `paths` (or everything) and commit. Returns the sha, or None when there
        was nothing to commit or git was unavailable."""
        if not self.initialised:
            return None
        if paths is None:
            staged = self._try("add", "-A")
        else:
            staged = self._try("add", "-A", "--", *paths)
        if staged is None:
            return None
        if self._returncode("diff", "--cached", "--quiet") == 0:
            return None  # exit 0 → nothing staged
        if self._try("commit", "-m", message, "--no-verify") is None:
            return None
        return self.head()

    def commit_external_changes(self, message: str) -> str | None:
        """Commit edits made outside Loom (Obsidian, the user's editor) — 03 §8.8."""
        return self.commit(message, None)

    def tag(self, name: str) -> bool:
        return self._try("tag", "-f", name) is not None

    def revert(self, sha: str, path: str | None = None) -> str | None:
        """Undo one commit, or one path within it. Returns the new commit sha."""
        if not self.initialised:
            return None
        if path is None:
            if self._try("revert", "--no-edit", sha) is None:
                return None
            return self.head()
        target = f"{sha}^"
        restored = self._try("checkout", target, "--", path)
        if restored is None:
            # The path did not exist before `sha` (it was created there): remove it.
            if self._try("rm", "-f", "--ignore-unmatch", "--", path) is None:
                return None
        return self.commit(f"loom: revert {path} to {sha[:8]}", [path])

    # -- reading ----------------------------------------------------------------
    def log(self, path: str | None = None, n: int = 50) -> list[dict[str, Any]]:
        args = ["log", f"-n{int(n)}", f"--pretty=format:%H{_SEP}%aI{_SEP}%an{_SEP}%s", "--name-only"]
        if path:
            args += ["--", path]
        out = self._try(*args)
        if not out:
            return []
        entries: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for line in out.splitlines():
            if _SEP in line:
                sha, ts, author, message = line.split(_SEP, 3)
                current = {"sha": sha, "ts": ts, "author": author, "message": message, "paths": []}
                entries.append(current)
            elif line.strip() and current is not None:
                current["paths"].append(line.strip())
        return entries

    def show(self, sha: str, path: str) -> str:
        out = self._try("show", f"{sha}:{path}")
        return out or ""

    def diff(self, sha: str, path: str | None = None) -> str:
        args = ["show", "--format=%H%n%an%n%aI%n%s%n", "--patch", sha]
        if path:
            args += ["--", path]
        return self._try(*args) or ""

    def status_porcelain(self) -> list[str]:
        """Paths with uncommitted changes (vault-relative, forward slashes)."""
        out = self._try("status", "--porcelain", "-uall")
        if not out:
            return []
        paths = []
        for line in out.splitlines():
            if len(line) < 4:
                continue
            entry = line[3:]
            if " -> " in entry:  # rename: report the destination
                entry = entry.split(" -> ", 1)[1]
            paths.append(entry.strip().strip('"').replace("\\", "/"))
        return paths

    # -- async wrappers ---------------------------------------------------------
    async def a_commit(self, message: str, paths: list[str] | None = None) -> str | None:
        return await asyncio.to_thread(self.commit, message, paths)

    async def a_commit_external_changes(self, message: str) -> str | None:
        return await asyncio.to_thread(self.commit_external_changes, message)

    async def a_log(self, path: str | None = None, n: int = 50) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.log, path, n)

    async def a_show(self, sha: str, path: str) -> str:
        return await asyncio.to_thread(self.show, sha, path)

    async def a_diff(self, sha: str, path: str | None = None) -> str:
        return await asyncio.to_thread(self.diff, sha, path)

    async def a_revert(self, sha: str, path: str | None = None) -> str | None:
        return await asyncio.to_thread(self.revert, sha, path)

    async def a_status_porcelain(self) -> list[str]:
        return await asyncio.to_thread(self.status_porcelain)
