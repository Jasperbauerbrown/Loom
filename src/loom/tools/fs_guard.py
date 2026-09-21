"""Filesystem guard: step 3 of the dispatcher pipeline (03 §9.3).

One question, asked of every path argument of every tool: is this path inside an
allowed root, and is it protected? The guard never denies — it either passes or returns
an `ask`, which the dispatcher turns into an approval the user answers. An "outside the
roots" ask carries the root that would fix it, so answering `allow_always` widens the
roots instead of nagging again.

Roots are the `roots` table plus the conversation's own `roots` column, plus the vault
and artifacts directories, which are always allowed (the knowledge and artifact tools
live there).

Knowledge tools take *vault-relative* paths and register with `path_params=()`; they
guard inside their own handlers. Nothing here is special-cased for them.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .. import db
from .registry import Tool

log = logging.getLogger(__name__)


_MSYS_DRIVE = re.compile(r"^/(?:cygdrive/)?([A-Za-z])(?=/|$)")


def _from_msys(raw: str) -> str:
    """Git Bash and Cygwin spell `C:/x` as `/c/x`; the model reads those in shell output
    and hands them back to the file tools. Left alone, `abspath` would make `/c/x` into
    `<cwd drive>:/c/x` — a real directory nobody asked for (seen 2026-09-21)."""
    match = _MSYS_DRIVE.match(raw)
    if not match:
        return raw
    return f"{match.group(1).upper()}:" + (raw[match.end():] or "/")


def normalise(path: str, cwd: str | None = None) -> str:
    """Absolute, `..`-resolved, forward slashes, case preserved. Relative paths resolve
    against `cwd` (default: the process working directory). Lexical on purpose: a
    symlink must not be able to move a path out of a root behind the user's back, and
    the path need not exist yet (fs_write creates it). On Windows, MSYS/Cygwin drive
    spellings (`/c/Users/...`) are read as `C:/Users/...`."""
    raw = os.path.expanduser(str(path).strip().strip('"').strip("'"))
    raw = raw.replace("\\", "/")
    if os.name == "nt":
        raw = _from_msys(raw)
    if not os.path.isabs(raw) and cwd:
        raw = os.path.join(str(cwd).replace("\\", "/"), raw)
    out = os.path.normpath(os.path.abspath(raw)).replace("\\", "/")
    if len(out) > 3 and out.endswith("/"):
        out = out[:-1]
    return out


def _key(path: str) -> str:
    """Comparison key: Windows paths compare case-insensitively, POSIX exactly."""
    return path.lower() if os.name == "nt" else path


def _under(path: str, prefix: str) -> bool:
    p, q = _key(path), _key(prefix.rstrip("/"))
    return p == q or p.startswith(q + "/")


# ---------------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------------
def global_roots(app: Any) -> list[str]:
    return [normalise(r["path"]) for r in app.db.execute("SELECT path FROM roots ORDER BY path")]


def effective_roots(app: Any, conversation: dict[str, Any] | None = None) -> list[str]:
    """Global roots + this conversation's extra roots + vault + artifacts, deduped."""
    out: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if not value:
            return
        path = normalise(str(value))
        if _key(path) not in seen:
            seen.add(_key(path))
            out.append(path)

    for path in global_roots(app):
        add(path)
    if conversation:
        try:
            extra = db.loads(conversation.get("roots"), []) or []
        except Exception:
            extra = []
        for path in extra if isinstance(extra, list) else []:
            add(path)
    add(app.vault_dir)
    add(app.artifacts_dir)
    return out


def add_root(app: Any, path: str, added_by: str = "user") -> str:
    """Insert a root (idempotent) and tell every surface. Returns the stored form."""
    normalised = normalise(path)
    app.db.execute(
        "INSERT OR IGNORE INTO roots(path, added_at, added_by) VALUES (?, ?, ?)",
        (normalised, db.now_iso(), added_by),
    )
    app.bus.emit("settings.changed", {"keys": ["roots"]})
    return normalised


def remove_root(app: Any, path: str) -> bool:
    cur = app.db.execute("DELETE FROM roots WHERE path=?", (normalise(path),))
    if cur.rowcount:
        app.bus.emit("settings.changed", {"keys": ["roots"]})
    return bool(cur.rowcount)


# ---------------------------------------------------------------------------------
# Protected paths
# ---------------------------------------------------------------------------------
def protected_paths(app: Any) -> list[str]:
    return [normalise(r["path"]) for r in app.db.execute("SELECT path FROM protected_paths ORDER BY path")]


def is_protected(app: Any, path: str) -> bool:
    """True when `path` is a protected file or sits under a protected directory."""
    target = normalise(path)
    return any(_under(target, p) for p in protected_paths(app))


def add_protected(app: Any, path: str) -> str:
    normalised = normalise(path)
    app.db.execute("INSERT OR IGNORE INTO protected_paths(path) VALUES (?)", (normalised,))
    app.bus.emit("settings.changed", {"keys": ["protected_paths"]})
    return normalised


def remove_protected(app: Any, path: str) -> bool:
    cur = app.db.execute("DELETE FROM protected_paths WHERE path=?", (normalise(path),))
    if cur.rowcount:
        app.bus.emit("settings.changed", {"keys": ["protected_paths"]})
    return bool(cur.rowcount)


# ---------------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------------
@dataclass
class GuardOk:
    paths: list[str] = field(default_factory=list)


@dataclass
class GuardAsk:
    reason: str
    options: dict[str, Any] = field(default_factory=dict)


def _values(args: dict[str, Any], name: str) -> list[str]:
    value = args.get(name)
    if isinstance(value, str) and value.strip():
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def _suggested_root(path: str) -> str:
    """The narrowest root that would let this call through: the file's directory, or the
    directory itself when the path is (or will be) one."""
    if os.path.isdir(path):
        return path
    parent = os.path.dirname(path)
    return normalise(parent) if parent else path


def check(app: Any, conversation: dict[str, Any] | None, tool: Tool, args: dict[str, Any]) -> GuardOk | GuardAsk:
    """Resolve every `path_params` argument and judge it. First problem wins, so the
    user answers one question at a time."""
    if not tool.path_params:
        return GuardOk()
    roots = effective_roots(app, conversation)
    base = args.get("cwd") if isinstance(args.get("cwd"), str) else None
    base = base or (roots[0] if roots else None)
    resolved: list[str] = []
    for name in tool.path_params:
        for raw in _values(args, name):
            path = normalise(raw, cwd=base)
            resolved.append(path)
            if is_protected(app, path):
                return GuardAsk(reason=f"protected path {path}", options={})
            if not any(_under(path, root) for root in roots):
                return GuardAsk(
                    reason=f"{path} is outside allowed roots",
                    options={"add_root": _suggested_root(path)},
                )
    return GuardOk(paths=resolved)


def within_roots(app: Any, conversation: dict[str, Any] | None, path: str) -> bool:
    """For handlers that resolve extra paths themselves (fs_search's walk, shell's cwd)."""
    target = normalise(path)
    return any(_under(target, root) for root in effective_roots(app, conversation))


def describe(app: Any, conversation: dict[str, Any] | None = None) -> dict[str, Any]:
    """Roots and protected paths as the classifier prompt renders them."""
    return {"roots": effective_roots(app, conversation), "protected": protected_paths(app)}
