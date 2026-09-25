"""Knowledge tools: the model's hands on the vault and the transcript (03 §8.4).

Every handler is a thin shell over `app.vault` / `app.index` so the rules live in one
place: the vault validates paths (`..` and absolute paths are refused there, which is
why `path_params` is empty — there is nothing for the filesystem guard to add), stamps
frontmatter, refuses relative dates, commits to git and updates the index.

A `VaultError` is an explanation the model can act on, so it is returned verbatim as an
error result rather than raised: "`old` appears 3 times in devices/ender-3.md" tells the
model exactly what to resend.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ... import prompts
from ...knowledge.vault import VaultError
from ..registry import Tool, ToolContext, ToolResult

DEFAULT_READ_LINES = 400


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------------
# knowledge_search
# ---------------------------------------------------------------------------------
async def knowledge_search_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult.error("query is required")
    note_type = str(args.get("type") or "").strip() or None
    k = _clamp(args.get("k"), 8, 1, 50)
    rows = await ctx.app.index.search(query, note_type, k)
    if not rows:
        return ToolResult(content=f"no notes matched {query!r}", meta={"count": 0})
    lines = [f"{r['path']} — {r['title']} [{r['type']}]\n    {r['snippet']}".rstrip() for r in rows]
    return ToolResult(content="\n".join(lines), meta={"count": len(rows), "paths": [r["path"] for r in rows]})


# ---------------------------------------------------------------------------------
# knowledge_read
# ---------------------------------------------------------------------------------
def _apply_pin(ctx: ToolContext, path: str, pin: Any) -> str:
    """`pin=true` keeps the note in this conversation's context every turn; `pin=false`
    drops it; omitted leaves the list alone (03 §6.7, 2026-09-22). Runs after a
    successful read only, on a fresh row so two pins in one turn both stick. Returns the
    line appended to the result, or an empty string."""
    if pin is None:
        return ""
    from ...conversation import pins

    conv = ctx.app.conversation(ctx.conversation_id) or ctx.conversation
    if pin:
        pins.pin(ctx.app, conv, path)
        return "\n[pinned: this note is in your context every turn; knowledge_read(path, pin=false) to drop it]"
    pins.unpin(ctx.app, conv, path)
    return "\n[unpinned]"


async def knowledge_read_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        note = await asyncio.to_thread(ctx.app.vault.read, str(args.get("path") or ""))
    except VaultError as exc:
        return ToolResult.error(str(exc))
    lines = note.raw.splitlines()
    total = len(lines)
    offset = max(0, _clamp(args.get("offset"), 0, 0, 10_000_000))
    limit = _clamp(args.get("limit"), DEFAULT_READ_LINES, 1, 10_000)
    pin_line = _apply_pin(ctx, note.path, args.get("pin"))
    if offset >= total and total:
        return ToolResult(content=f"[{note.path} has {total} lines; offset {offset} is past the end]{pin_line}")
    chunk = lines[offset : offset + limit]
    text = "\n".join(chunk)
    truncated = False
    # The page is `conversation.tool_output_cap`, the number the dispatcher spills at;
    # knowledge_read is a paged tool, so page plus footer is never spilled. A fixed 8000
    # here was the off-by-footer trap that looped artifact_recall (2026-09-22).
    char_cap = int(ctx.app.settings.get("conversation.tool_output_cap"))
    if len(text) > char_cap:
        text = text[:char_cap]
        # Drop the line the cut landed in so the next page opens with it whole; keep it
        # only when it is the sole line, so a page always makes progress.
        cut = text.rfind("\n")
        if cut > 0:
            text = text[:cut]
        chunk = text.splitlines()
        truncated = True
    end = offset + len(chunk)
    footer = ""
    if end < total or truncated:
        footer = f"\n[lines {offset + 1}–{end} of {total}; knowledge_read(path, offset={end}) for more]"
    if note.error:
        footer += f"\n[this note's frontmatter is invalid: {note.error}]"
    return ToolResult(
        content=text + footer + pin_line,
        meta={"path": note.path, "lines": total, "offset": offset, "end": end, "type": note.type},
    )


# ---------------------------------------------------------------------------------
# knowledge_write
# ---------------------------------------------------------------------------------
_PAST = {"create": "created", "append": "appended to", "replace": "replaced in", "rethink": "rewrote"}


async def knowledge_write_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("path") or "")
    op = str(args.get("op") or "create").strip().lower()
    content = args.get("content")
    if content is None and op == "replace":
        # `replace` reads as old→new, and that is what a model sends (the raylib thread
        # sent `old` + `new`, 2026-09-24). Accepting the alias costs a line; the prompt
        # names both field names beside the op.
        content = args.get("new")
    if content is None:
        return ToolResult.error("content is required" + (" (or `new` for op=replace)" if op == "replace" else ""))
    frontmatter = args.get("frontmatter")
    if frontmatter is not None and not isinstance(frontmatter, dict):
        return ToolResult.error("frontmatter must be a mapping of keys to values")
    try:
        # Synchronous on purpose: a write updates the index, and SQLite is used from the
        # event-loop thread only (charter decision, 2026-09-20). File and git work here
        # is local and short.
        note = ctx.app.vault.write(
            path,
            str(content),
            op=op,
            old=(None if args.get("old") is None else str(args.get("old"))),
            frontmatter=frontmatter,
            reason=(None if args.get("reason") is None else str(args.get("reason"))),
            actor="loom",
        )
    except VaultError as exc:
        return ToolResult.error(str(exc))
    return ToolResult(
        content=f"{_PAST.get(op, op)} {note.path}",
        meta={"path": note.path, "op": op, "type": note.type},
    )


# ---------------------------------------------------------------------------------
# knowledge_links
# ---------------------------------------------------------------------------------
async def knowledge_links_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    direction = str(args.get("direction") or "out").strip().lower()
    if direction not in ("out", "in"):
        return ToolResult.error("direction must be 'out' or 'in'")
    try:
        path = ctx.app.vault.norm(str(args.get("path") or ""))
    except VaultError as exc:
        return ToolResult.error(str(exc))
    if not ctx.app.vault.exists(path):
        return ToolResult.error(f"no such note: {path}")
    paths = ctx.app.index.links(path, direction)
    if not paths:
        word = "links to no notes" if direction == "out" else "has no backlinks"
        return ToolResult(content=f"{path} {word}", meta={"count": 0})
    lines = []
    for target in paths:
        meta = ctx.app.index._note_row(target) or {"title": target, "type": "?"}
        lines.append(f"{target} — {meta['title']} [{meta['type']}]")
    return ToolResult(content="\n".join(lines), meta={"count": len(paths), "paths": paths})


# ---------------------------------------------------------------------------------
# procedure_load
# ---------------------------------------------------------------------------------
async def procedure_load_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    name = str(args.get("name") or "").strip()
    if not name:
        return ToolResult.error("name is required")
    try:
        body = await asyncio.to_thread(ctx.app.vault.load_procedure, name)
    except VaultError as exc:
        return ToolResult.error(str(exc))
    return ToolResult(content=body, meta={"name": name})


# ---------------------------------------------------------------------------------
# transcript_search
# ---------------------------------------------------------------------------------
async def transcript_search_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult.error("query is required")
    conversation = str(args.get("conversation") or "").strip() or None
    k = _clamp(args.get("k"), 8, 1, 50)
    rows = ctx.app.index.transcript_search(query, conversation, k)
    if not rows:
        return ToolResult(content=f"no transcript events matched {query!r}", meta={"count": 0})
    want_neighbours = bool(args.get("neighbours"))
    blocks = []
    for row in rows:
        title = row["title"] or row["conversation_id"]
        head = f"[{row['ts']}] {title} ({row['kind']}, event {row['event_id']})"
        if want_neighbours:
            around = ctx.app.index.neighbours(row["event_id"], 2)
            body = "\n".join(f"  {n['kind']}: {n['content'][:400]}" for n in around)
        else:
            body = f"  {row['snippet']}"
        blocks.append(f"{head}\n{body}")
    return ToolResult(content="\n\n".join(blocks), meta={"count": len(rows)})


# ---------------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------------
NOTE_TYPES = ["device", "project", "person", "connector", "course", "topic", "procedure", "core", "log"]


def tools() -> list[Tool]:
    return [
        Tool(
            name="knowledge_search",
            description=prompts.load("tools/knowledge_search"),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look for, in ordinary words."},
                    "type": {"type": "string", "description": "Restrict to one note type.", "enum": NOTE_TYPES},
                    "k": {"type": "integer", "description": "How many results.", "default": 8, "minimum": 1, "maximum": 50},
                },
                "required": ["query"],
            },
            handler=knowledge_search_handler,
            read_only=True,
        ),
        Tool(
            name="knowledge_read",
            description=prompts.load("tools/knowledge_read"),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Vault-relative path, e.g. devices/ender-3.md."},
                    "offset": {"type": "integer", "description": "Zero-based first line.", "default": 0},
                    "limit": {"type": "integer", "description": "Lines to return.", "default": DEFAULT_READ_LINES},
                    "pin": {"type": "boolean", "description": "true keeps this note in your context every turn, false drops it; omitted leaves it as is."},
                },
                "required": ["path"],
            },
            handler=knowledge_read_handler,
            read_only=True,
            paged=True,
        ),
        Tool(
            name="knowledge_write",
            description=prompts.load("tools/knowledge_write"),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Vault-relative path ending in .md."},
                    "op": {
                        "type": "string",
                        "description": "How to write.",
                        "enum": ["create", "append", "replace", "rethink"],
                        "default": "create",
                    },
                    "content": {"type": "string", "description": "Markdown to write. Absolute dates only."},
                    "old": {"type": "string", "description": "For replace: the exact text to swap out (must be unique)."},
                    "new": {"type": "string", "description": "For replace: alias of content — the text that replaces old."},
                    "frontmatter": {
                        "type": "object",
                        "description": "Frontmatter keys to merge (aliases, tags, type, root).",
                        "additionalProperties": True,
                    },
                    "reason": {"type": "string", "description": "For rethink: why the old note was wrong."},
                },
                # `content` is required by every op but is checked in the handler, which
                # is the one place that knows `new` is its alias under op=replace.
                "required": ["path", "op"],
            },
            handler=knowledge_write_handler,
        ),
        Tool(
            name="knowledge_links",
            description=prompts.load("tools/knowledge_links"),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Vault-relative path of the note."},
                    "direction": {
                        "type": "string",
                        "description": "out = links from this note; in = backlinks.",
                        "enum": ["out", "in"],
                        "default": "out",
                    },
                },
                "required": ["path", "direction"],
            },
            handler=knowledge_links_handler,
            read_only=True,
        ),
        Tool(
            name="procedure_load",
            description=prompts.load("tools/procedure_load"),
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Procedure name as listed in context."}},
                "required": ["name"],
            },
            handler=procedure_load_handler,
            read_only=True,
        ),
        Tool(
            name="transcript_search",
            description=prompts.load("tools/transcript_search"),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words to look for in past messages."},
                    "conversation": {"type": "string", "description": "Restrict to one conversation id."},
                    "k": {"type": "integer", "description": "How many matches.", "default": 8, "minimum": 1, "maximum": 50},
                    "neighbours": {
                        "type": "boolean",
                        "description": "Also return the two events either side of each match.",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
            handler=transcript_search_handler,
            read_only=True,
        ),
    ]
