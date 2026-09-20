"""Integrations: MCP servers (tier 2) and native connectors (tier 3) — charter §5.8.

`install` attaches both managers and discovers the connector packages (synchronous:
rows and imports only). `start` connects every enabled MCP server — each in its own
task, so a wedged server delays nothing — and applies every connector row. `stop`
closes the clients and stops the surfaces.

    app.mcp         loom.connectors.mcp.manager.Manager
    app.connectors  loom.connectors.loader.Loader
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Connector, ConnectorError, Event, EventSource, Surface, SurfaceApi
from .loader import Loader
from .mcp.manager import Manager

log = logging.getLogger(__name__)

__all__ = ["Connector", "ConnectorError", "Event", "EventSource", "Surface", "SurfaceApi", "Loader", "Manager"]


def install(app: Any) -> None:
    app.mcp = Manager(app)
    app.connectors = Loader(app)
    app.connectors.discover()


async def start(app: Any) -> None:
    await app.mcp.start()
    await app.connectors.start()


async def stop(app: Any) -> None:
    try:
        await app.connectors.stop()
    except Exception:  # pragma: no cover - shutdown is best effort
        log.exception("connectors: stop failed")
    try:
        await app.mcp.stop()
    except Exception:  # pragma: no cover
        log.exception("mcp: stop failed")
