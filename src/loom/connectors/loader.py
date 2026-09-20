"""`app.connectors`: discover, configure and run native connectors (03 §10.2).

One row per package in `<repo>/connectors/` (override with `LOOM_CONNECTORS_DIR`, an
os.pathsep-separated list). Discovery imports each `__init__.py` in a try/except and
reads `CONNECTOR`; an import error disables that connector, records `last_error` and
posts a setup item — never fatal to the backend (03 §17).

Row status, one per class:

    disabled        `enabled = 0`
    needs_config    enabled, but a required setting in `config_schema` has no value
    loaded          tools registered, event task running, surface started
    error           the package would not import, or its event task keeps failing

A discovered connector starts `enabled = 1`: a package the user put in `connectors/` is
there because they want it, and one whose schema has unset required fields stops at
`needs_config` with a setup item rather than running half-configured.

Enable/disable and config edits take effect immediately; `reload(name)` re-imports the
module so editing a connector needs no restart (a restart is always safe).
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from .. import db
from ..bootstrap import repo_root
from .base import Connector, ConnectorError, Event, SurfaceApi, missing_required, validate_config
from .mcp.manager import post_setup_item, tools_list, write_connector_note

log = logging.getLogger(__name__)

MODULE_PREFIX = "loom_connectors"
BACKOFF_START = 5.0
BACKOFF_MAX = 300.0


class Loader:
    def __init__(self, app: Any):
        self.app = app
        self.connectors: dict[str, Connector] = {}  # name → the imported CONNECTOR
        self.modules: dict[str, ModuleType] = {}
        self.surfaces: dict[str, tuple[Any, SurfaceApi]] = {}
        self._attempts: dict[str, int] = {}

    # -- discovery ---------------------------------------------------------------
    def dirs(self) -> list[Path]:
        override = os.environ.get("LOOM_CONNECTORS_DIR")
        if override:
            return [Path(p) for p in override.split(os.pathsep) if p.strip()]
        root = repo_root()
        return [root / "connectors"] if root is not None else []

    def discover(self) -> None:
        """Import every package and make sure each has a row. Synchronous: this is the
        `install` half, before any task exists."""
        for directory in self.dirs():
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if not (entry.is_dir() and (entry / "__init__.py").is_file()) or entry.name.startswith((".", "_")):
                    continue
                self._import(entry.name, entry)

    def _import(self, name: str, path: Path) -> Connector | None:
        self._ensure_row(name)
        try:
            module = _load_module(name, path)
            connector = getattr(module, "CONNECTOR", None)
            if not isinstance(connector, Connector):
                raise ConnectorError(f"{path / '__init__.py'} does not export CONNECTOR = Connector(...)")
            if connector.name != name:
                connector.name = name  # the folder is the identity; the row keys on it
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("connectors: %s did not import (%s)", name, reason)
            self.connectors.pop(name, None)
            self.modules.pop(name, None)
            self._emit(self._set(name, status="error", last_error=reason))
            post_setup_item(self.app, f"connector:{name}", f"Connector '{name}' failed to load", reason)
            return None
        self.connectors[name] = connector
        self.modules[name] = module
        return connector

    def _ensure_row(self, name: str) -> dict[str, Any]:
        row = self.row(name)
        if row is None:
            self.app.db.execute(
                "INSERT INTO connectors(name, enabled, config, status, updated_at) VALUES (?, 1, '{}', 'disabled', ?)",
                (name, db.now_iso()),
            )
            row = self.row(name)
        return row  # type: ignore[return-value]

    # -- rows --------------------------------------------------------------------
    def rows(self) -> list[dict[str, Any]]:
        return [self._decode(r) for r in self.app.db.execute("SELECT * FROM connectors ORDER BY name")]

    def row(self, name: str) -> dict[str, Any] | None:
        record = self.app.db.execute("SELECT * FROM connectors WHERE name=?", (name,)).fetchone()
        return self._decode(record) if record is not None else None

    def _decode(self, record: Any) -> dict[str, Any]:
        row = db.row_to_dict(record) or {}
        row["config"] = db.loads(row.get("config"), {})
        row["enabled"] = bool(row.get("enabled"))
        return row

    def _set(self, name: str, **fields: Any) -> dict[str, Any] | None:
        fields["updated_at"] = db.now_iso()
        assignments = ", ".join(f"{k}=?" for k in fields)
        self.app.db.execute(f"UPDATE connectors SET {assignments} WHERE name=?", (*fields.values(), name))
        return self.describe(name)

    def describe(self, name: str) -> dict[str, Any] | None:
        """The row the API and the WS event carry: row + everything the Settings form
        needs to render the connector without a second request (charter §2.2)."""
        row = self.row(name)
        if row is None:
            return None
        connector = self.connectors.get(name)
        config = row["config"] if isinstance(row.get("config"), dict) else {}
        row["description"] = connector.description if connector else ""
        row["config_schema"] = connector.config_schema if connector else {"type": "object", "properties": {}}
        row["facets"] = connector.facets if connector else []
        row["tools"] = [t.name for t in self._tools(connector, config)] if connector else []
        row["available"] = connector is not None
        row["missing_config"] = missing_required(row["config_schema"], config) if connector else []
        return row

    def status(self) -> list[dict[str, Any]]:
        return [self.describe(r["name"]) for r in self.rows()]  # type: ignore[misc]

    def _emit(self, row: dict[str, Any] | None) -> None:
        if row is not None:
            self.app.bus.emit("connector.status", row)

    # -- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        for row in self.rows():
            await self.apply(row["name"])

    async def stop(self) -> None:
        for name in list(self.connectors):
            await self._teardown(name)

    async def apply(self, name: str) -> dict[str, Any] | None:
        """Bring one connector into the state its row asks for. The only path that
        registers tools, starts the event task and starts the surface."""
        row = self.row(name)
        if row is None:
            return None
        await self._teardown(name)
        connector = self.connectors.get(name)
        if connector is None:
            return self.describe(name)  # import failed; the row already says error
        if not row["enabled"]:
            return self._emit_and_return(self._set(name, status="disabled", last_error=None))

        config = row["config"] if isinstance(row.get("config"), dict) else {}
        missing = missing_required(connector.config_schema, config)
        if missing:
            reason = f"needs configuration: {', '.join(missing)}"
            post_setup_item(self.app, f"connector:{name}", f"Connector '{name}' needs configuration", reason)
            return self._emit_and_return(self._set(name, status="needs_config", last_error=reason))

        try:
            config = validate_config(connector.config_schema, config)
        except ConnectorError as exc:
            post_setup_item(self.app, f"connector:{name}", f"Connector '{name}' has an invalid configuration", str(exc))
            return self._emit_and_return(self._set(name, status="needs_config", last_error=str(exc)))

        tools = self._tools(connector, config)
        for tool in tools:
            self.app.tools.register(tool, replace=True)

        if connector.events is not None:
            self.app.spawn(f"connector:{name}:events", self._events(name, config))
        if connector.surface is not None:
            api = SurfaceApi(self.app, name)
            self.surfaces[name] = (connector.surface, api)
            self.app.spawn(f"connector:{name}:surface", self._surface(name, connector.surface, api, config))

        if connector.setup is not None:
            try:
                item = connector.setup(config)
            except Exception as exc:
                log.warning("connectors: %s setup() raised (%s)", name, exc)
                item = None
            if isinstance(item, dict) and item.get("title"):
                post_setup_item(self.app, f"connector:{name}:setup", str(item["title"]), str(item.get("body", "")))

        write_connector_note(self.app, name, "connector", connector.description, tools_list([t.to_json() for t in tools]))
        return self._emit_and_return(self._set(name, status="loaded", last_error=None))

    def _emit_and_return(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        self._emit(row)
        return row

    def _tools(self, connector: Connector | None, config: dict[str, Any]) -> list[Any]:
        """The connector's tools, renamed `<connector>_<tool>` and sourced to it."""
        if connector is None:
            return []
        import dataclasses

        out = []
        try:
            declared = connector.tools_for(config)
        except Exception as exc:
            log.warning("connectors: %s tools() raised (%s)", connector.name, exc)
            return []
        prefix = f"{connector.name}_"
        for tool in declared:
            name = tool.name if tool.name.startswith(prefix) else f"{prefix}{tool.name}"
            out.append(dataclasses.replace(tool, name=name, source=f"connector:{connector.name}"))
        return out

    async def _teardown(self, name: str) -> None:
        self.app.tools.unregister_source(f"connector:{name}")
        for suffix in ("events", "surface"):
            task = self.app.tasks.get(f"connector:{name}:{suffix}")
            if task is not None and not task.done():
                task.cancel()
        entry = self.surfaces.pop(name, None)
        if entry is not None:
            surface, api = entry
            api.close()
            try:
                await surface.stop()
            except Exception as exc:  # pragma: no cover - a stubborn surface
                log.warning("connectors: %s surface.stop() raised (%s)", name, exc)
        self._attempts.pop(name, None)

    # -- tasks -------------------------------------------------------------------
    async def _events(self, name: str, config: dict[str, Any]) -> None:
        """Forward the connector's events to the router forever; a source that raises
        restarts with backoff after marking the connector `error` (03 §17)."""
        connector = self.connectors.get(name)
        if connector is None or connector.events is None:
            return
        delay = BACKOFF_START
        while not getattr(self.app, "shutting_down", False):
            try:
                async for event in connector.events.events(config):
                    self._route(name, config, event)
                return  # a source that finishes has nothing more to say
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = f"event source failed: {type(exc).__name__}: {exc}"
                log.warning("connectors: %s %s", name, reason)
                self._emit(self._set(name, status="error", last_error=reason))
                post_setup_item(self.app, f"connector:{name}", f"Connector '{name}' stopped", reason)
                await asyncio.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX)

    def _route(self, name: str, config: dict[str, Any], event: Event) -> None:
        """The one way a connector event enters Loom: the trigger router (03 §11.2),
        imported lazily so the packages stay independent."""
        from ..triggers.router import route_connector_event

        result = route_connector_event(
            self.app,
            name,
            event.title,
            event.content,
            event.meta or {"kind": event.kind},
            conversation_id=config.get("conversation_id"),
        )
        if inspect.isawaitable(result):
            self.app.spawn(f"connector:{name}:route:{id(event)}", result)

    async def _surface(self, name: str, surface: Any, api: SurfaceApi, config: dict[str, Any]) -> None:
        try:
            await surface.start(api, config)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = f"surface failed: {type(exc).__name__}: {exc}"
            log.warning("connectors: %s %s", name, reason)
            self._emit(self._set(name, status="error", last_error=reason))
            post_setup_item(self.app, f"connector:{name}", f"Connector '{name}' surface stopped", reason)

    # -- API ---------------------------------------------------------------------
    async def set_config(self, name: str, config: dict[str, Any]) -> dict[str, Any] | None:
        row = self.row(name)
        if row is None:
            raise KeyError(name)
        connector = self.connectors.get(name)
        if connector is not None:
            config = validate_config(connector.config_schema, config)  # raises ConnectorError
        self._set(name, config=db.dumps(config))
        return await self.apply(name)

    async def enable(self, name: str, enabled: bool = True) -> dict[str, Any] | None:
        if self.row(name) is None:
            raise KeyError(name)
        self._set(name, enabled=int(bool(enabled)))
        return await self.apply(name)

    async def reload(self, name: str) -> dict[str, Any] | None:
        """Stop, re-execute the package, start again (03 §10.2: no restart required).

        Re-execution rather than `importlib.reload`: the module is loaded from its file
        by spec, so the same path works for the first import and every later one."""
        if self.row(name) is None:
            raise KeyError(name)
        await self._teardown(name)
        path = self._path(name)
        if path is None:
            reason = "the connector package is no longer in the connectors folder"
            self.connectors.pop(name, None)
            self.modules.pop(name, None)
            self._emit(self._set(name, status="error", last_error=reason))
            post_setup_item(self.app, f"connector:{name}", f"Connector '{name}' is missing", reason)
            return self.describe(name)
        for module_name in [m for m in sys.modules if m == f"{MODULE_PREFIX}.{name}" or m.startswith(f"{MODULE_PREFIX}.{name}.")]:
            sys.modules.pop(module_name, None)
        if self._import(name, path) is None:  # records status=error and posts the item
            return self.describe(name)
        return await self.apply(name)

    def _path(self, name: str) -> Path | None:
        for directory in self.dirs():
            candidate = directory / name
            if (candidate / "__init__.py").is_file():
                return candidate
        return None


def _load_module(name: str, path: Path) -> ModuleType:
    """Import `<dir>/<name>/__init__.py` as `loom_connectors.<name>` without putting the
    connectors folder on sys.path (two connectors may have colliding helper names)."""
    module_name = f"{MODULE_PREFIX}.{name}"
    if MODULE_PREFIX not in sys.modules:
        package = ModuleType(MODULE_PREFIX)
        package.__path__ = []  # type: ignore[attr-defined]
        sys.modules[MODULE_PREFIX] = package
    spec = importlib.util.spec_from_file_location(module_name, path / "__init__.py", submodule_search_locations=[str(path)])
    if spec is None or spec.loader is None:
        raise ConnectorError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module
