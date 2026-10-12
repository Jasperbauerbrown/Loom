"""Composition root. `App` holds every subsystem; `build_app` wires them; `start`/`stop`
run the async lifecycle in the order 03 §3.2 prescribes.

Ownership rule for parallel work: each package exposes `install(app)` (synchronous
wiring: register tools, attach objects) and optionally `async start(app)` /
`async stop(app)`. `build_app` calls `install` in dependency order; `start` calls the
async starts in the same order. Add your package to the two lists below with a
one-line edit; do not rewrite this file.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, db
from .bus import Bus
from .cancel import CancelFlags
from .config import Settings, app_home
from .models.types import ModelClient
from .tools.registry import Registry

log = logging.getLogger(__name__)


@dataclass
class App:
    home: Path
    db: sqlite3.Connection
    settings: Settings
    bus: Bus
    models: ModelClient
    tools: Registry
    version: str = __version__
    started_at: str = ""
    shutting_down: bool = False
    first_run: bool = False
    # What `serve` actually bound (a CLI --port beats the setting); the harness context
    # block tells the model so it never mistakes its own port for someone else's.
    bind_host: str = ""
    bind_port: int = 0

    # Attached by packages in install(): typed as Any so packages can land independently.
    secrets: Any = None  # loom.secrets.Store
    artifacts: Any = None  # loom.artifacts.Store
    processes: Any = None  # loom.processes.Processes
    vault: Any = None  # loom.knowledge.vault.Vault
    index: Any = None  # loom.knowledge.index.Index
    watcher: Any = None  # loom.knowledge.watcher.Watcher
    scheduler: Any = None  # loom.triggers.scheduler.Scheduler
    mcp: Any = None  # loom.connectors.mcp.Manager
    connectors: Any = None  # loom.connectors.loader.Loader
    maintenance: Any = None  # loom.knowledge.maintenance.Maintenance
    supervisor: Any = None  # loom.promote.SupervisorClient

    # Runtime state shared across packages.
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)  # named background tasks
    turn_locks: dict[str, asyncio.Lock] = field(default_factory=dict)  # per conversation id
    cancel_flags: CancelFlags = field(default_factory=CancelFlags)  # per-conversation cancel signals (loom.cancel)
    extras: dict[str, Any] = field(default_factory=dict)  # ad-hoc attachments (tests, probes)

    # -- paths ------------------------------------------------------------------
    @property
    def state_db(self) -> Path:
        return self.home / "state.db"

    @property
    def vault_dir(self) -> Path:
        return self.home / "vault"

    @property
    def artifacts_dir(self) -> Path:
        return self.home / "artifacts"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def releases_dir(self) -> Path:
        return self.home / "releases"

    @property
    def snapshots_dir(self) -> Path:
        return self.home / "snapshots"

    # -- conversation helpers used everywhere ---------------------------------
    def desk_id(self) -> str:
        row = self.db.execute("SELECT id FROM conversations WHERE kind='desk' LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("desk conversation missing; bootstrap did not run")
        return row["id"]

    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        return db.row_to_dict(row)

    def lock_for(self, conversation_id: str) -> asyncio.Lock:
        lock = self.turn_locks.get(conversation_id)
        if lock is None:
            lock = self.turn_locks[conversation_id] = asyncio.Lock()
        return lock

    def spawn(self, name: str, coro: Any) -> asyncio.Task:
        """Track a background task by name; a second spawn with the same name cancels the
        first. Exceptions are logged, never lost."""
        old = self.tasks.get(name)
        if old is not None and not old.done():
            old.cancel()
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks[name] = task

        def _done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                log.error("task %s failed: %r", name, exc, exc_info=exc)
            if self.tasks.get(name) is t:
                self.tasks.pop(name, None)

        task.add_done_callback(_done)
        return task

    def has_model(self, role: str = "desk") -> bool:
        row = self.db.execute("SELECT preset FROM model_roles WHERE role=?", (role,)).fetchone()
        return bool(row and row["preset"])


# ---------------------------------------------------------------------------------
# Install order. Each entry is "package.module" exposing install(app) [+ start/stop].
# ---------------------------------------------------------------------------------
INSTALL_ORDER: list[str] = [
    "loom.secrets",
    "loom.artifacts",
    "loom.processes",
    "loom.tools.native",
    "loom.knowledge",
    "loom.knowledge.maintenance",
    "loom.conversation",
    "loom.triggers",
    "loom.connectors",
    "loom.promote",
]


def _import(name: str) -> Any:
    import importlib

    return importlib.import_module(name)


def build_app(home: Path | None = None, *, models: ModelClient | None = None) -> App:
    """Create APP_HOME if needed, migrate, bootstrap defaults on first run, wire every
    package. Pure synchronous; async subsystems start in `start(app)`."""
    home = Path(home) if home is not None else app_home()
    home.mkdir(parents=True, exist_ok=True)
    first_run = not (home / "state.db").exists()
    conn = db.connect(home / "state.db")
    applied = db.migrate(conn)
    if applied:
        log.info("migrated state.db to version %s", applied[-1])
    settings = Settings.load(conn)
    app = App(home=home, db=conn, settings=settings, bus=Bus(), models=models or _NoModels(), tools=Registry(), first_run=first_run)
    app.started_at = db.now_iso()

    from . import bootstrap

    bootstrap.ensure(app, applied)  # idempotent: desk row, default policies, roots, protected paths, vault skeleton

    if models is None:
        from .models.client import Client

        app.models = Client(app)

    for name in INSTALL_ORDER:
        mod = _import(name)
        mod.install(app)
    return app


async def start(app: App) -> None:
    """Async startup in 03 §3.2 order: each package's `start(app)` if it has one."""
    for name in INSTALL_ORDER:
        mod = _import(name)
        fn = getattr(mod, "start", None)
        if fn is not None:
            await fn(app)
    app.bus.emit("system.started", {"version": app.version, "first_run": app.first_run})


async def stop(app: App) -> None:
    app.shutting_down = True
    for name in reversed(INSTALL_ORDER):
        mod = _import(name)
        fn = getattr(mod, "stop", None)
        if fn is not None:
            try:
                await fn(app)
            except Exception:
                log.exception("stop failed for %s", name)
    grace = float(app.settings.get("conversation.shutdown_grace_s"))
    pending = [t for t in app.tasks.values() if not t.done()]
    if pending:
        done, still = await asyncio.wait(pending, timeout=grace)
        for t in still:
            t.cancel()
    try:
        app.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    app.db.close()


class _NoModels:
    """Placeholder until the real client is attached; every call is a fatal outcome."""

    async def stream(self, request: Any, priority: int = 0):  # type: ignore[override]
        from .models.types import Result

        yield Result(outcome="fatal", error="no model client")

    async def chat(self, request: Any, priority: int = 0):  # type: ignore[override]
        from .models.types import Result

        return Result(outcome="fatal", error="no model client")

    async def embed(self, texts: list[str], priority: int = 2) -> list[list[float]]:
        from .models.types import ModelError

        raise ModelError("fatal", "no model client")

    async def list_models(self, provider: str) -> list[str]:
        return []

    def queue_state(self) -> dict[str, Any]:
        return {"depth": 0, "running": []}
