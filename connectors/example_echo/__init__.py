"""The reference native connector (03 §10.2): one tool, one optional event source.

It exists to be read — it is the smallest complete example of the contract — and to be
what the loader's tests run against. Everything it does is harmless: `echo` returns its
argument, and the event source only ticks when `emit_events` is switched on.

Copy this folder to start a real one; see `connectors/README.md`.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from loom.connectors.base import Connector, Event
from loom.tools.registry import Tool, ToolContext, ToolResult

CONFIG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "emit_events": {
            "type": "boolean",
            "default": False,
            "title": "Emit events",
            "description": "Send a tick event to the desk every interval. Off by default.",
        },
        "interval_s": {
            "type": "number",
            "default": 3600,
            "title": "Interval (seconds)",
            "description": "How often to emit a tick when events are on.",
        },
    },
    "required": [],
}


async def echo(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return ToolResult(content=str(args.get("text", "")))


class Ticks:
    """An event source: `events(config)` is an async iterator of `Event`. Cancellation
    is the normal way it ends; raising restarts it with backoff."""

    async def events(self, config: dict[str, Any]) -> AsyncIterator[Event]:
        if not config.get("emit_events"):
            return
        interval = max(float(config.get("interval_s") or 3600), 0.01)
        count = 0
        while True:
            await asyncio.sleep(interval)
            count += 1
            yield Event(
                kind="tick",
                title=f"example_echo tick {count}",
                content=f"The example connector has been running for {count} interval(s).",
                meta={"count": count},
            )


CONNECTOR = Connector(
    name="example_echo",
    description="Reference connector: echoes text back, and can emit a periodic tick event.",
    config_schema=CONFIG_SCHEMA,
    tools=[
        Tool(
            name="echo",  # registered as `example_echo_echo`
            description="Return the text you pass in, unchanged. A connectivity check.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "Text to echo back."}},
                "required": ["text"],
            },
            handler=echo,
            read_only=True,
        )
    ],
    events=Ticks(),
    surface=None,
)
