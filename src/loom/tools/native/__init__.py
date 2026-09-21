"""Native tools: the capabilities Loom has on its own machine, before any MCP server or
connector is configured (03 §9.2).

`install(app)` registers them into `app.tools`. Registration is idempotent (`replace`)
so a reload does not raise; the registry is the single source of what exists, and the
permission table in Settings renders from it.

Tools owned by other packages — the knowledge, conversation, schedule, item and promote
tools — register themselves from their own `install`.
"""

from __future__ import annotations

from typing import Any

from . import fs, http, misc, screen, shell


def tools() -> list[Any]:
    return [*fs.tools(), *shell.tools(), *http.tools(), *screen.tools(), *misc.tools()]


def install(app: Any) -> None:
    for tool in tools():
        app.tools.register(tool, replace=True)
