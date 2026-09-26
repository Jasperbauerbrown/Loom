"""Filesystem guard: step 3 of the dispatcher pipeline (03 §9.3).

One question, asked of every path a call names — its `path_params` arguments, plus
whatever a registered extractor finds inside them (`register_paths`, which is how
fs_patch's paths, buried in a unified diff, are judged): is this path inside an
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

import functools
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import db
from .registry import Tool

log = logging.getLogger(__name__)


_MSYS_DRIVE = re.compile(r"^/(?:cygdrive/)?([A-Za-z])(?=/|$)")
_MOUNT_LINE = re.compile(r"^(?P<win>.+?) on (?P<posix>/\S*) type ")


def _from_msys(raw: str) -> str:
    """Git Bash and Cygwin spell `C:/x` as `/c/x`; the model reads those in shell output
    and hands them back to the file tools. Left alone, `abspath` would make `/c/x` into
    `<cwd drive>:/c/x` — a real directory nobody asked for (seen 2026-09-21)."""
    match = _MSYS_DRIVE.match(raw)
    if not match:
        return raw
    return f"{match.group(1).upper()}:" + (raw[match.end():] or "/")


def _derived_mounts(bash: str) -> dict[str, str]:
    """The Git for Windows defaults, derived from where its bash lives: `/` is the
    install root, `/bin` is `<root>/usr/bin`, `/tmp` is the user's temp directory
    (`usertemp` in its fstab). The fallback when asking the shell itself fails."""
    parent = os.path.dirname(bash.replace("\\", "/"))
    root = os.path.dirname(parent)
    if os.path.basename(parent).lower() == "bin" and os.path.basename(root).lower() == "usr":
        root = os.path.dirname(root)  # <root>/usr/bin/bash.exe
    root = root.replace("\\", "/")
    temp = (os.environ.get("TEMP") or os.environ.get("TMP") or "").replace("\\", "/")
    mounts = {"/": root, "/bin": f"{root}/usr/bin"}
    if temp:
        mounts["/tmp"] = temp
    return mounts


def _ask_mounts(bash: str) -> dict[str, str]:
    """The live mount table, from `mount` run by the very bash the shell tool runs — so
    a customised `/etc/fstab` is honoured too. Drive mounts (`C: on /c`) are left to
    `_from_msys`, which needs no shell."""
    import subprocess

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = subprocess.run(
        [bash, "-c", "mount"], capture_output=True, text=True, timeout=10, creationflags=flags
    ).stdout
    mounts: dict[str, str] = {}
    for line in out.splitlines():
        match = _MOUNT_LINE.match(line.strip())
        if not match:
            continue
        win, posix = match.group("win").replace("\\", "/"), match.group("posix")
        if re.fullmatch(r"[A-Za-z]:/?", win):
            continue
        mounts[posix] = win.rstrip("/") if len(win) > 3 else win
    return mounts


@functools.lru_cache(maxsize=1)
def msys_mounts() -> dict[str, str]:
    """`{posix mount point: Windows path}` for the Git Bash the shell tool runs, asked
    once per process. Empty off Windows or without Git Bash — then a POSIX absolute
    path keeps its old meaning (the current drive). `/tmp` in Git Bash is the user's
    temp directory, not `C:/tmp`; a guard reading it lexically disagreed with the shell
    about which file a thread's download named (2026-09-25). Tests monkeypatch this."""
    if os.name != "nt":
        return {}
    from .native.shell import find_git_bash  # lazy: shell imports this module

    bash = find_git_bash()
    if not bash:
        return {}
    mounts = _derived_mounts(bash)
    try:
        asked = _ask_mounts(bash)
    except Exception as exc:
        log.warning("fs guard: could not read Git Bash's mount table (%r); using defaults", exc)
        asked = {}
    if "/" in asked:
        return asked
    mounts.update(asked)
    return mounts


def _from_mounts(raw: str, mounts: dict[str, str]) -> str:
    """A POSIX absolute path (`/tmp/x`, `/usr/bin`, `/`) read through the mount table,
    longest mount point first; anything not under a narrower mount lands under `/`,
    which is what Git Bash does with `/foo`. UNC (`//host/share`) is left alone."""
    if not raw.startswith("/") or raw.startswith("//") or not mounts:
        return raw
    for point in sorted(mounts, key=len, reverse=True):
        if point == "/":
            return mounts[point].rstrip("/") + raw
        if raw == point or raw.startswith(point + "/"):
            return mounts[point] + raw[len(point):]
    return raw


def normalise(path: str, cwd: str | None = None) -> str:
    """Absolute, `..`-resolved, forward slashes, case preserved. Relative paths resolve
    against `cwd` (default: the process working directory). Lexical on purpose: a
    symlink must not be able to move a path out of a root behind the user's back, and
    the path need not exist yet (fs_write creates it). On Windows, MSYS/Cygwin drive
    spellings (`/c/Users/...`) are read as `C:/Users/...`, and every other POSIX
    absolute path through Git Bash's mount table (`/tmp/x` → the user's temp dir), so
    the guard and the shell name the same file."""
    raw = os.path.expanduser(str(path).strip().strip('"').strip("'"))
    raw = raw.replace("\\", "/")
    if os.name == "nt":
        raw = _from_msys(raw)
        if raw.startswith("/") and not raw.startswith("//"):
            try:
                raw = _from_mounts(raw, msys_mounts())
            except Exception:  # never raise: an unreadable mount table means no mounts
                pass
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
    protected: bool = False  # a protected path always asks, even under desk auto-approve


def _values(args: dict[str, Any], name: str) -> list[str]:
    value = args.get(name)
    if isinstance(value, str) and value.strip():
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


# A tool whose paths are not in its arguments (fs_patch's paths live inside the unified
# diff it is handed) registers an extractor here, and the guard judges what it returns
# exactly like a `path_params` value. `Tool.path_params` cannot express "the paths
# inside a diff" and the registry is not the place to learn about diffs, so this is the
# smallest general mechanism: one function per tool name, one judging loop for both.
PathExtractor = Callable[[dict[str, Any]], list[str]]
_EXTRA_PATHS: dict[str, PathExtractor] = {}


def register_paths(tool_name: str, extractor: PathExtractor) -> None:
    """`extractor(args) -> list[str]`: the paths this call names beyond `path_params`.
    Idempotent; the fs package registers fs_patch's when its tools are built."""
    _EXTRA_PATHS[tool_name] = extractor


def extra_paths(tool_name: str, args: dict[str, Any]) -> list[str]:
    """What the registered extractor finds, or nothing. An extractor that raises (a
    patch that will not parse) yields no paths — and the handler refuses the call with
    the same parse error, so nothing unguarded is ever written."""
    extractor = _EXTRA_PATHS.get(tool_name)
    if extractor is None:
        return []
    try:
        found = extractor(args)
    except Exception as exc:
        log.warning("fs guard: path extractor for %s raised: %r", tool_name, exc)
        return []
    return [p for p in (found or []) if isinstance(p, str) and p.strip()]


def _suggested_root(path: str) -> str:
    """The narrowest root that would let this call through: the file's directory, or the
    directory itself when the path is (or will be) one."""
    if os.path.isdir(path):
        return path
    parent = os.path.dirname(path)
    return normalise(parent) if parent else path


def check(app: Any, conversation: dict[str, Any] | None, tool: Tool, args: dict[str, Any]) -> GuardOk | GuardAsk:
    """Resolve every path this call names — its `path_params` arguments and whatever a
    registered extractor finds inside them — and judge each the same way. First problem
    wins, so the user answers one question at a time."""
    raw_paths: list[str] = []
    for name in tool.path_params:
        raw_paths += _values(args, name)
    raw_paths += extra_paths(tool.name, args)
    if not raw_paths:
        return GuardOk()
    roots = effective_roots(app, conversation)
    base = args.get("cwd") if isinstance(args.get("cwd"), str) else None
    base = base or (roots[0] if roots else None)
    resolved: list[str] = []
    for raw in raw_paths:
        path = normalise(raw, cwd=base)
        resolved.append(path)
        if is_protected(app, path):
            return GuardAsk(reason=f"protected path {path}", options={}, protected=True)
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
