"""The `process` tool (03 §9.6): list, read the log of, or stop a background process
started with `shell(background=true)`. `list` and `logs` are read-only; `logs` pages
its own output like `artifact_recall`, so it is never spilled.
"""

from __future__ import annotations

from typing import Any

from ... import prompts
from ...processes import duration
from ..registry import Tool, ToolContext, ToolResult

ACTIONS = ("list", "logs", "stop")
LIST_MAX = 50


def _list(ctx: ToolContext) -> ToolResult:
    app = ctx.app
    rows = app.processes.list()
    rows.sort(key=lambda r: (r["status"] != "running", r["started_at"]), reverse=False)
    # running first (False sorts first), then newest: re-sort the two groups
    running = [r for r in rows if r["status"] == "running"]
    rest = [r for r in rows if r["status"] != "running"]
    running.sort(key=lambda r: r["started_at"], reverse=True)
    rest.sort(key=lambda r: r["started_at"], reverse=True)
    rows = (running + rest)[:LIST_MAX]
    if not rows:
        return ToolResult("no background processes")
    lines = ["id | title | status | pid | started | ended | exit | log bytes | conversation"]
    for r in rows:
        owner = app.conversation(r["conversation_id"]) or {}
        owner_title = owner.get("title") or (owner.get("goal") or "")[:60] or r["conversation_id"]
        exit_code = "" if r.get("exit_code") is None else str(r["exit_code"])
        lines.append(
            f'{r["id"]} | {r["title"]} | {r["status"]} | {r.get("pid") or ""} | {r["started_at"]} | '
            f'{r.get("ended_at") or ""} | {exit_code} | {r.get("log_bytes", 0)} | {owner_title}'
        )
    return ToolResult("\n".join(lines))


def _logs(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    process_id = str(args.get("id") or "").strip()
    if not process_id:
        return ToolResult.error("id is required for logs")
    offset = args.get("offset")
    limit = args.get("limit")
    page = ctx.app.processes.read_log(
        process_id,
        None if offset is None else int(offset),
        None if limit is None else int(limit),
    )
    if page is None:
        return ToolResult.error(f"no process {process_id}")
    text = page["text"]
    total, start, end = page["total"], page["offset"], page["end"]
    if end < total:
        footer = f'\n[chars {start}–{end} of {total}; process(action="logs", id="{process_id}", offset={end}) for more]'
    elif offset is None and start > 0:
        footer = f"\n[showing the last {end - start} chars of {total}; offset=0 for the start]"
    else:
        footer = ""
    if not text and total == 0:
        text = "(no output yet)"
    return ToolResult(text + footer, meta={"id": process_id, "offset": start, "end": end, "total": total})


async def _stop(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    process_id = str(args.get("id") or "").strip()
    if not process_id:
        return ToolResult.error("id is required for stop")
    before = ctx.app.processes.get(process_id)
    if before is None:
        return ToolResult.error(f"no process {process_id}")
    if before["status"] != "running":
        return ToolResult(f'process {process_id} ("{before["title"]}") is not running: {before["status"]} after {duration(before)}')
    row = await ctx.app.processes.stop(process_id, actor=f"conversation:{ctx.conversation_id}")
    return ToolResult(f'stopped process {process_id} ("{(row or before)["title"]}")', meta={"id": process_id})


async def process_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if getattr(ctx.app, "processes", None) is None:
        return ToolResult.error("no process store is available")
    action = str(args.get("action") or "").strip()
    if action == "list":
        return _list(ctx)
    if action == "logs":
        return _logs(ctx, args)
    if action == "stop":
        return await _stop(ctx, args)
    return ToolResult.error("action must be one of " + ", ".join(ACTIONS))


def tools() -> list[Tool]:
    return [
        Tool(
            name="process",
            description=prompts.load("tools/process"),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(ACTIONS), "description": "list, logs or stop."},
                    "id": {"type": "string", "description": "Process id from shell(background=true) or list."},
                    "offset": {"type": "integer", "description": "logs: character offset to start at; omitted = the tail."},
                    "limit": {"type": "integer", "description": "logs: characters to return; default and maximum are the tool output cap."},
                },
                "required": ["action"],
            },
            handler=process_handler,
            read_only_for=lambda args: (args or {}).get("action") in ("list", "logs"),
            paged=True,
        )
    ]
