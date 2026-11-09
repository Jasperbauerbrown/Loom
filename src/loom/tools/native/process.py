"""The `process` tool (03 §9.6): list, read the log of, wait on, or stop a background
process started with `shell(background=true)`. `list`, `logs` and `wait` are read-only;
`logs` pages its own output like `artifact_recall`, so it is never spilled.

`wait` blocks inside one step: it polls the row and the log file (so it works for a
process re-attached after a restart), and answers at the deadline the dispatcher computes
for the call, which sits a grace margin below the dispatcher's own cut-off.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from ... import prompts
from ...processes import duration
from ..registry import Tool, ToolContext, ToolResult

ACTIONS = ("list", "logs", "wait", "stop")
LIST_MAX = 50
WAIT_POLL_S = 0.5
QUOTE_CHARS = 300  # of the matching line quoted back


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
    limit = args.get("limit")
    start_at: int | None = None
    if not args.get("tail"):
        # `since` and `offset` mean the same thing; `since` is the name the footer
        # hands back, `offset` is kept so nothing that works today breaks.
        raw = args.get("since")
        if raw is None:
            raw = args.get("offset")
        if raw is not None:
            try:
                start_at = int(raw)
            except (TypeError, ValueError):
                return ToolResult.error("since must be a non-negative integer")
            if start_at < 0:
                return ToolResult.error("since must be a non-negative integer")
    page = ctx.app.processes.read_log(
        process_id,
        start_at,
        None if limit is None else int(limit),
    )
    if page is None:
        return ToolResult.error(f"no process {process_id}")
    text = page["text"]
    total, start, end = page["total"], page["offset"], page["end"]
    if start > total:
        # `since` past the end: nothing to show, and the footer should name the log's
        # actual end, not the offset that was asked for.
        start = end = total
    if end < total:
        footer = f'\n[chars {start}–{end} of {total}; process(action="logs", id="{process_id}", since={end}) for what follows, tail=true for the end]'
    else:
        footer = f'\n[chars {start}–{end} of {total}; process(action="logs", id="{process_id}", since={end}) for what is written next]'
    if not text and total == 0:
        text = "(no output yet)"
    return ToolResult(text + footer, meta={"id": process_id, "offset": start, "end": end, "total": total})


def _read(row: dict[str, Any]) -> str:
    try:
        return Path(row["log_path"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


async def _wait(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from .. import dispatch  # lazy: dispatch imports the tool registry

    app = ctx.app
    process_id = str(args.get("id") or "").strip()
    if not process_id:
        return ToolResult.error("id is required for wait")
    if app.processes.get(process_id) is None:
        return ToolResult.error(f"no process {process_id}")
    pattern = args.get("until")
    rx = None
    if pattern:
        try:
            rx = re.compile(str(pattern), re.MULTILINE)
        except re.error as exc:
            return ToolResult.error(f"until is not a valid regular expression: {exc}")
    try:
        since = int(args.get("since") or 0)
    except (TypeError, ValueError):
        return ToolResult.error("since must be a non-negative integer")
    if since < 0:
        return ToolResult.error("since must be a non-negative integer")
    timeout = dispatch.timeout_for(app, app.tools.get("process"), args)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    nomatch = "; no match for until" if rx is not None else ""
    while True:
        row = app.processes.get(process_id)
        if row is None:
            return ToolResult.error(f"no process {process_id}")
        text = _read(row)
        total = len(text)
        footer = f'[log is {total} chars; process(action="logs", id="{process_id}", since={total}) for what is written next]'
        if rx is not None:
            m = rx.search(text[since:])
            if m:
                at = since + m.start()
                line_start = text.rfind("\n", 0, at) + 1
                line_end = text.find("\n", at)
                line_end = total if line_end < 0 else line_end
                nxt = min(total, line_end + 1)
                return ToolResult(
                    f"matched at char {at}: {text[line_start:line_end][:QUOTE_CHARS]}\n"
                    f'[process {process_id} is {row["status"]}; process(action="logs", id="{process_id}", since={nxt}) for what follows]',
                    meta={"id": process_id, "matched": True, "offset": at, "end": nxt, "total": total},
                )
        if row["status"] != "running":
            code = row.get("exit_code")
            code_text = "" if code is None else f" with code {code}"
            return ToolResult(
                f'process {process_id} ("{row["title"]}") is no longer running: {row["status"]}{code_text} after {duration(row)}{nomatch}. '
                f"Last output:\n{app.processes._tail(row)}\n{footer}",
                meta={"id": process_id, "status": row["status"], "total": total},
            )
        remaining = deadline - loop.time()
        if remaining <= 0:
            return ToolResult(
                f'process {process_id} ("{row["title"]}") is still running after {timeout:g}s{nomatch}. '
                f"Last output:\n{app.processes._tail(row)}\n{footer}",
                meta={"id": process_id, "status": "running", "timed_out": True, "total": total},
            )
        await asyncio.sleep(min(WAIT_POLL_S, remaining))  # a user cancel lands here as CancelledError


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
    if action == "wait":
        return await _wait(ctx, args)
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
                    "action": {"type": "string", "enum": list(ACTIONS), "description": "list, logs, wait or stop."},
                    "id": {"type": "string", "description": "Process id from shell(background=true) or list."},
                    "tail": {"type": "boolean", "description": "logs: read the last page from the current end of the log, however much it has grown. Wins over since/offset; also the default when none of tail/since/offset are given."},
                    "since": {"type": "integer", "description": "logs: character offset to read from, forward — hand it back the offset the previous read's footer named. wait: where matching starts (default 0)."},
                    "offset": {"type": "integer", "description": "logs: same meaning as since, kept for back-compat. offset/since is ignored if tail is set."},
                    "until": {"type": "string", "description": "wait: regular expression matched against the log text (a line printed before the wait started still matches)."},
                    "timeout_s": {"type": "number", "description": "wait: seconds to wait before answering that it is still running; honoured up to the configured ceiling."},
                    "limit": {"type": "integer", "description": "logs: characters to return; default and maximum are the tool output cap."},
                },
                "required": ["action"],
            },
            handler=process_handler,
            read_only_for=lambda args: (args or {}).get("action") in ("list", "logs", "wait"),
            paged=True,
        )
    ]
