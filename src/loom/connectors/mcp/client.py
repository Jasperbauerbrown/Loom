"""The MCP client proper: the handful of methods 03 §10.1 puts in v1.

    client = McpClient(transport, name="canvas")
    await client.initialize()            # → server info + capabilities, then initialized
    tools = await client.list_tools()    # paginated via nextCursor
    out = await client.call_tool("search", {"q": "x"}, timeout=30)   # {"ok", "content"}
    await client.ping()
    await client.close()

`call_tool` flattens MCP content blocks into the one string a `ToolResult` carries: text
parts concatenated, non-text parts noted as `[image image/png, 1234 bytes]` so the model
knows something was returned that it cannot see. `isError: true` becomes `ok=False` —
a tool-level failure is an ordinary result the model reads, not an exception (03 §17).

Resources, prompts, sampling and elicitation are not implemented; a server request for
any of them is refused by the transport with JSON-RPC -32601 rather than hanging.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Callable

from .transports import McpError, Transport

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
MAX_PAGES = 50


class McpClient:
    def __init__(self, transport: Transport, name: str = "mcp", version: str = "1"):
        self.transport = transport
        self.name = name
        self.version = version
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.protocol_version: str = ""
        self.initialized = False
        self._on_tools_changed: Callable[[], None] | None = None
        transport.on_notification(self._notification)

    # -- lifecycle ---------------------------------------------------------------
    async def initialize(self, timeout: float = 30.0) -> dict[str, Any]:
        await self.transport.open()
        result = await self.transport.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "loom", "version": self.version},
            },
            timeout=timeout,
        )
        result = result if isinstance(result, dict) else {}
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self.protocol_version = str(result.get("protocolVersion") or "")
        await self.transport.notify("notifications/initialized", {})
        self.initialized = True
        return result

    async def ping(self, timeout: float = 10.0) -> None:
        await self.transport.request("ping", {}, timeout=timeout)

    async def close(self) -> None:
        self.initialized = False
        await self.transport.close()

    def on_tools_changed(self, callback: Callable[[], None] | None) -> None:
        self._on_tools_changed = callback

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "notifications/tools/list_changed" and self._on_tools_changed is not None:
            self._on_tools_changed()

    # -- tools -------------------------------------------------------------------
    async def list_tools(self, timeout: float = 30.0) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            result = await self.transport.request("tools/list", params, timeout=timeout)
            result = result if isinstance(result, dict) else {}
            page = result.get("tools")
            if isinstance(page, list):
                tools.extend(t for t in page if isinstance(t, dict) and t.get("name"))
            cursor = result.get("nextCursor") or None
            if not cursor:
                return tools
        log.warning("mcp: %s: stopped paginating tools/list after %d pages", self.name, MAX_PAGES)
        return tools

    async def call_tool(self, tool: str, args: dict[str, Any] | None = None, timeout: float = 300.0) -> dict[str, Any]:
        result = await self.transport.request("tools/call", {"name": tool, "arguments": args or {}}, timeout=timeout)
        result = result if isinstance(result, dict) else {}
        content = flatten_content(result.get("content"))
        if not content and result.get("structuredContent") is not None:
            import json

            content = json.dumps(result["structuredContent"], indent=2, sort_keys=True)
        return {"ok": not bool(result.get("isError")), "content": content, "raw": result}


def flatten_content(parts: Any) -> str:
    """MCP content blocks → the one string a tool result carries."""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return "" if parts is None else str(parts)
    out: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            out.append(str(part))
            continue
        kind = part.get("type")
        if kind == "text":
            out.append(str(part.get("text", "")))
        elif kind in ("image", "audio"):
            out.append(f"[{kind} {part.get('mimeType', 'application/octet-stream')}, {_size(part.get('data'))} bytes]")
        elif kind == "resource":
            resource = part.get("resource") or {}
            uri = resource.get("uri", "")
            if isinstance(resource.get("text"), str):
                out.append(f"[resource {uri}]\n{resource['text']}")
            else:
                out.append(f"[resource {uri} {resource.get('mimeType', '')}, {_size(resource.get('blob'))} bytes]".replace("  ", " "))
        elif kind == "resource_link":
            out.append(f"[resource_link {part.get('uri', '')} {part.get('name', '')}]".rstrip(" ]") + "]")
        else:
            out.append(f"[{kind or 'unknown'} content]")
    return "\n".join(s for s in out if s)


def _size(data: Any) -> int:
    if not isinstance(data, str):
        return 0
    try:
        return len(base64.b64decode(data, validate=False))
    except Exception:
        return len(data)


__all__ = ["McpClient", "McpError", "flatten_content", "PROTOCOL_VERSION"]
