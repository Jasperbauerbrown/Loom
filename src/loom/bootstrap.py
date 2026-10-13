"""First-run bootstrap and idempotent defaults (03 §3.3 steps 1–2).

`ensure(app)` is safe to call on every start: it inserts what is missing and touches
nothing that exists. The provider probe (step 2's network part) is async and lives in
`models.client.probe_local_ollama`, called from `serve` startup.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from . import db
from .config import detect_timezone
from .ids import ulid

log = logging.getLogger(__name__)

# 03 §9.2 default policies for native tools. Anything not listed falls back to the
# dispatcher's rule: readOnlyHint → allow, else ask (fail closed).
DEFAULT_TOOL_POLICIES: dict[str, str] = {
    "fs_read": "allow",
    "fs_list": "allow",
    "fs_search": "allow",
    "fs_write": "allow",
    "fs_edit": "allow",
    "fs_patch": "allow",
    "shell": "classify",
    "process": "allow",
    "http": "classify",
    "screenshot": "allow",
    "knowledge_search": "allow",
    "knowledge_read": "allow",
    "knowledge_write": "allow",
    "knowledge_links": "allow",
    "procedure_load": "allow",
    "transcript_search": "allow",
    "artifact_recall": "allow",
    "status_update": "allow",
    "conversation_create": "allow",
    "conversation_send": "allow",
    "conversation_read": "allow",
    "conversation_list": "allow",
    "conversation_finish": "allow",
    "schedule_create": "allow",
    "schedule_list": "allow",
    "schedule_delete": "allow",
    "item_post": "allow",
    "secret_set": "ask",
    "tool_search": "allow",
    "promote": "ask",
}

# Pseudo-tool policy keys for path classes (03 §8.4: core/ writes default to ask).
DEFAULT_PATH_POLICIES: dict[str, str] = {
    "knowledge_write:core/": "ask",
}


def default_protected_paths(repo_root: Path | None, home: Path) -> list[str]:
    paths = [
        "supervisor/",
        "src/loom/tools/permissions.py",
        "src/loom/tools/fs_guard.py",
        "src/loom/tools/shell_prefilter.py",
        "src/loom/promote.py",
        "src/loom/secrets.py",
        str(home / "secrets.json"),
        str(home / "supervisor.token"),
    ]
    if repo_root is not None:
        paths = [str(repo_root / p) if not os.path.isabs(p) else p for p in paths]
    return [_norm(p) for p in paths]


# Migration 0008 gave protected paths a mode; these rows are seeded with `writes`.
WRITES_PROTECTED_MIGRATION = 8


def default_write_protected_paths(vault: Path) -> list[str]:
    """Paths that ask only for writes: a procedure's scripts, which threads run by name
    (2026-10-12). Reading and running them stays free; editing one asks."""
    return [_norm(str(Path(vault) / "procedures" / "*" / "scripts"))]


def repo_root() -> Path | None:
    """The checkout this package runs from (for protected paths), or None when
    installed as a wheel without the repo around it."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() and (parent / "src" / "loom").exists():
            return parent
    return None


def checkout_root() -> Path | None:
    """The development checkout, which is what protected paths name. A release runs from
    a worktree of the supervisor's mirror (`.git` is a file), whose origin is the checkout
    it was built from; anything else is its own checkout. Only a worktree costs a git call."""
    root = repo_root()
    if root is None or not (root / ".git").is_file():
        return root
    try:
        proc = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(root),
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return root
    url = proc.stdout.strip() if proc.returncode == 0 else ""
    target = Path(url) if url else None
    if target is not None and (target / "pyproject.toml").exists() and (target / "src" / "loom").exists():
        return target
    return root


def _norm(p: str) -> str:
    return str(Path(p).expanduser()).replace("\\", "/")


def ensure(app, applied: list[int] | None = None) -> None:  # type: ignore[no-untyped-def]
    """`applied`: the migrations this start applied (`db.migrate`'s answer), so a row a
    migration's arrival seeds is seeded once and a row the person deleted stays gone."""
    conn = app.db
    now = db.now_iso()
    with db.transaction(conn):
        # Desk row: exactly one.
        if conn.execute("SELECT 1 FROM conversations WHERE kind='desk'").fetchone() is None:
            conn.execute(
                "INSERT INTO conversations(id, kind, title, role, status, created_at, updated_at) "
                "VALUES (?, 'desk', 'Desk', 'desk', 'idle', ?, ?)",
                (ulid(), now, now),
            )
        # Roles rows exist even when unset so the UI can render five dropdowns.
        for role in ("desk", "worker", "maintenance", "utility", "embedding"):
            conn.execute("INSERT OR IGNORE INTO model_roles(role, preset) VALUES (?, NULL)", (role,))
        # Tool policies: insert defaults for known tools without overriding user edits.
        for tool, mode in {**DEFAULT_TOOL_POLICIES, **DEFAULT_PATH_POLICIES}.items():
            conn.execute(
                "INSERT OR IGNORE INTO tool_policies(tool, mode, updated_at) VALUES (?, ?, ?)",
                (tool, mode, now),
            )
        # Roots: the vault and the artifacts folder are always allowed. Everything else
        # is the person's call under Permissions (the projects-root setting that used to
        # be seeded here is gone, 2026-09-22).
        if conn.execute("SELECT COUNT(*) FROM roots").fetchone()[0] == 0:
            for p in (app.vault_dir, app.artifacts_dir):
                conn.execute(
                    "INSERT OR IGNORE INTO roots(path, added_at, added_by) VALUES (?, ?, 'bootstrap')",
                    (_norm(str(p)), now),
                )
        # Protected paths: seeded on a fresh install. The write-protected defaults also
        # arrive with migration 0008 on an existing install — once, on that start.
        fresh = conn.execute("SELECT COUNT(*) FROM protected_paths").fetchone()[0] == 0
        if fresh:
            for p in default_protected_paths(checkout_root(), app.home):
                conn.execute("INSERT OR IGNORE INTO protected_paths(path) VALUES (?)", (p,))
        if fresh or WRITES_PROTECTED_MIGRATION in (applied or []):
            for p in default_write_protected_paths(app.vault_dir):
                conn.execute("INSERT OR IGNORE INTO protected_paths(path, mode) VALUES (?, 'writes')", (p,))
        # Timezone: detected once; an ordinary setting the person can change later.
        if conn.execute("SELECT 1 FROM settings WHERE key='general.timezone'").fetchone() is None:
            app.settings.set(conn, "general.timezone", detect_timezone())
    for d in (app.vault_dir, app.artifacts_dir, app.logs_dir):
        d.mkdir(parents=True, exist_ok=True)
