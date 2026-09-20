"""The three small native tools that are about the assistant's own machinery:
`tool_search` (find tools that are not on screen), `secret_set` (take a credential out
of the conversation and put it in the keychain), `artifact_recall` (page through output
too long to keep in context).
"""

from __future__ import annotations

import json
from typing import Any

from ... import prompts
from ..registry import Tool, ToolContext, ToolResult


async def tool_search_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult.error("query is empty")
    k = max(1, int(args.get("k") or 10))
    found = ctx.app.tools.search(query, k)
    if not found:
        return ToolResult(
            content=f"[no tools match {query!r}; registered tools: {', '.join(ctx.app.tools.names())}]",
            meta={"count": 0},
        )
    exposed = ctx.extras.setdefault("exposed", set())
    exposed.update(tool.name for tool in found)
    definitions = [tool.to_json() for tool in found]
    body = json.dumps(definitions, indent=2, ensure_ascii=False)
    return ToolResult(
        content=f"{len(found)} tools loaded for the rest of this turn:\n{body}",
        meta={"count": len(found), "names": [t.name for t in found]},
    )


async def secret_set_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    name = str(args.get("name") or "").strip()
    value = args.get("value")
    if not isinstance(value, str) or not value:
        return ToolResult.error("value must be a non-empty string")
    store = ctx.app.secrets
    if store is None:
        return ToolResult.error("no secret store is available")
    try:
        store.set(name, value)
    except ValueError as exc:
        return ToolResult.error(str(exc))
    except Exception as exc:
        return ToolResult.error(f"could not store secret: {exc}")
    ctx.app.bus.emit("settings.changed", {"keys": ["secrets"]})
    return ToolResult(
        content=f"stored secret {name} ({len(value)} chars); use it as {{{{secret:{name}}}}}",
        meta={"name": name, "backend": store.backend},
    )


async def artifact_recall_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    store = ctx.app.artifacts
    if store is None:
        return ToolResult.error("no artifact store is available")
    artifact_id = str(args.get("id") or "").strip()
    if not artifact_id:
        return ToolResult.error("id is empty")
    text = store.read(artifact_id, int(args.get("offset") or 0), int(args.get("limit") or 8000))
    if text.startswith("error: "):
        return ToolResult(content=text, ok=False, meta={"id": artifact_id})
    return ToolResult(content=text, meta={"id": artifact_id})


def tools() -> list[Tool]:
    return [
        Tool(
            name="tool_search",
            description=prompts.load("tools/tool_search"),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What you want to do."},
                    "k": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
            handler=tool_search_handler,
            read_only=True,
        ),
        Tool(
            name="secret_set",
            description=prompts.load("tools/secret_set"),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name to store it under."},
                    "value": {"type": "string", "description": "The secret itself."},
                },
                "required": ["name", "value"],
            },
            handler=secret_set_handler,
        ),
        Tool(
            name="artifact_recall",
            description=prompts.load("tools/artifact_recall"),
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Artifact id from the result footer."},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 8000},
                },
                "required": ["id"],
            },
            handler=artifact_recall_handler,
            read_only=True,
        ),
    ]
