"""Threads (03 §6.7) and the conversation-facing tools: conversation_create / send /
read / list / finish, status_update, item_post. The surface contract exposed to the
model, so the desk spawns and talks to threads the same way a surface does.
"""

from __future__ import annotations

import datetime as _dt
import zoneinfo
from typing import Any

from .. import db, items, prompts
from ..ids import ulid
from ..tools.registry import Tool, ToolContext, ToolResult
from . import compaction, events


class ThreadError(Exception):
    pass


def create_thread(  # type: ignore[no-untyped-def]
    app,
    *,
    goal: str,
    parent_id: str | None = None,
    scope_note: str | None = None,
    project_name: str | None = None,
    roots: list[str] | None = None,
    preset: str | None = None,
    title: str | None = None,
    excerpt: str | None = None,
    role: str = "worker",
    created_by: str = "user",
) -> dict[str, Any]:
    """Persist the row, emit `conversation.created`, then post the goal (+ excerpt) to its
    inbox — the row exists before the first turn is scheduled."""
    goal = (goal or "").strip()
    if not goal:
        raise ThreadError("goal is required")
    parent_id = parent_id or app.desk_id()
    parent = app.conversation(parent_id)
    if parent is None:
        raise ThreadError(f"parent conversation {parent_id} not found")
    depth = int(parent.get("depth") or 0) + 1
    max_depth = int(app.settings.get("conversation.max_thread_depth"))
    if depth > max_depth:
        raise ThreadError(f"thread depth {depth} exceeds max_thread_depth ({max_depth})")
    if preset:
        row = app.db.execute("SELECT enabled FROM presets WHERE name=?", (preset,)).fetchone()
        if row is None or not row["enabled"]:
            raise ThreadError(f"preset {preset!r} does not exist or is disabled")
    if not scope_note and project_name:
        scope_note = app.vault.ensure_project_note(project_name)
    elif scope_note and not app.vault.exists(scope_note):
        stem = scope_note.rsplit("/", 1)[-1].removesuffix(".md")
        scope_note = app.vault.ensure_project_note(stem.replace("-", " "))
    roots = [_norm(r) for r in (roots or []) if r]
    cid = ulid()
    now = db.now_iso()
    title = (title or goal.splitlines()[0][:80]).strip()
    with db.transaction(app.db):
        app.db.execute(
            "INSERT INTO conversations(id, kind, parent_id, title, goal, scope_note, roots, depth, role, preset, status, created_at, updated_at) "
            "VALUES (?, 'thread', ?, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?)",
            (cid, parent_id, title, goal, scope_note, db.dumps(roots), depth, role, preset, now, now),
        )
    row = events.public_row(app, cid)
    app.bus.emit("conversation.created", row, cid)
    body = f"Goal: {goal}"
    if excerpt:
        body += f"\n\nContext from the spawning conversation:\n{excerpt.strip()}"
    from . import post

    if created_by == "user":
        post(app, cid, body, source="user")
    else:
        post(app, cid, body, source=f"conversation:{parent_id}", meta={"title": parent.get("title") or "Desk", "kind": parent["kind"]})
    return row  # type: ignore[return-value]


def _norm(p: str) -> str:
    from pathlib import Path

    return str(Path(p).expanduser()).replace("\\", "/")


def render_transcript(app, conv: dict[str, Any], limit: int = 30, before: int | None = None) -> str:  # type: ignore[no-untyped-def]
    evs = events.list_events(app, conv["id"], before=before, limit=limit, descending=True)
    evs.reverse()
    head = [
        f"conversation {conv['id']} — {conv.get('title') or ''}",
        f"kind: {conv['kind']}  status: {conv['status']}  updated: {conv.get('updated_at')}",
    ]
    if conv.get("goal"):
        head.append(f"goal: {conv['goal']}")
    if conv.get("scope_note"):
        head.append(f"scope note: {conv['scope_note']}")
    if conv.get("summary"):
        head.append(f"summary: {conv['summary'][:1500]}")
    body = compaction.render_for_summary([e for e in evs if e["kind"] != "thinking"])
    first_id = evs[0]["id"] if evs else None
    tail = f"\n[showing {len(evs)} events; conversation_read(before={first_id}) for earlier]" if first_id and len(evs) >= limit else ""
    return "\n".join(head) + "\n\n" + body + tail


def local_date(app) -> str:  # type: ignore[no-untyped-def]
    try:
        tz = zoneinfo.ZoneInfo(app.settings.get("general.timezone") or "UTC")
    except Exception:
        tz = _dt.timezone.utc
    return _dt.datetime.now(tz).strftime("%Y-%m-%d")


# -- tool handlers -----------------------------------------------------------------------

async def _create(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    app = ctx.app
    try:
        row = create_thread(
            app,
            goal=args.get("goal", ""),
            parent_id=ctx.conversation_id,
            scope_note=args.get("scope_note"),
            project_name=args.get("project_name"),
            roots=args.get("roots") or [],
            preset=args.get("preset"),
            title=args.get("title"),
            excerpt=args.get("excerpt"),
            created_by=f"conversation:{ctx.conversation_id}",
        )
    except ThreadError as exc:
        return ToolResult.error(str(exc))
    return ToolResult(f"created thread {row['id']} (\"{row['title']}\"); scope note: {row.get('scope_note') or 'none'}. It will report back when it needs input or finishes.")


async def _send(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    app = ctx.app
    target = args.get("conversation_id", "")
    other = app.conversation(target)
    if other is None:
        return ToolResult.error(f"conversation {target} not found")
    if other["id"] == ctx.conversation_id:
        return ToolResult.error("cannot send a message to yourself")
    from . import post

    me = ctx.conversation
    post(app, target, args.get("content", ""), source=f"conversation:{ctx.conversation_id}", meta={"title": me.get("title") or "Desk", "kind": me["kind"]})
    return ToolResult(f"sent to {target} ({other.get('title') or other['kind']}); it will be read at that conversation's next step")


async def _read(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    app = ctx.app
    other = app.conversation(args.get("conversation_id", ""))
    if other is None:
        return ToolResult.error(f"conversation {args.get('conversation_id')} not found")
    limit = max(1, min(int(args.get("limit") or 30), 200))
    before = args.get("before")
    return ToolResult(render_transcript(app, other, limit, int(before) if before else None))


async def _list(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    app = ctx.app
    sql = "SELECT id, kind, parent_id, title, goal, scope_note, status, depth, preset, updated_at FROM conversations"
    where, params = [], []
    if args.get("status"):
        where.append("status=?")
        params.append(args["status"])
    if not args.get("include_archived"):
        where.append("status!='archived'")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY kind, updated_at DESC"
    rows = app.db.execute(sql, params).fetchall()
    lines = ["id | kind | status | title | goal | scope note | parent | preset | updated"]
    for r in rows:
        lines.append(
            f"{r['id']} | {r['kind']} | {r['status']} | {r['title'] or ''} | {(r['goal'] or '')[:80]} | "
            f"{r['scope_note'] or ''} | {r['parent_id'] or ''} | {r['preset'] or ''} | {r['updated_at']}"
        )
    return ToolResult("\n".join(lines))


async def _finish(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    app = ctx.app
    conv = ctx.conversation
    if conv["kind"] != "thread":
        return ToolResult.error("conversation_finish applies to threads; the desk never finishes")
    summary = (args.get("summary") or "").strip()
    walkthrough = (args.get("walkthrough") or "").strip()
    state = args.get("state") or "done"
    if state not in ("done", "failed"):
        return ToolResult.error("state must be done or failed")
    if not summary or not walkthrough:
        return ToolResult.error("summary and walkthrough are both required")
    scope = conv.get("scope_note")
    note_path = None
    if scope:
        stem = scope.rsplit("/", 1)[-1].removesuffix(".md")
        note_path = f"projects/{stem}/walkthrough-{local_date(app)}.md"
        n = 2
        while app.vault.exists(note_path):
            note_path = f"projects/{stem}/walkthrough-{local_date(app)}-{n}.md"
            n += 1
        try:
            app.vault.write(
                note_path,
                f"# Walkthrough — {conv.get('title') or stem}\n\n{walkthrough}\n",
                op="create",
                frontmatter={"title": f"Walkthrough {local_date(app)}: {conv.get('title') or stem}", "type": "walkthrough", "conversation": conv["id"], "state": state},
            )
            link = note_path.removesuffix(".md")
            note = app.vault.read(scope)
            section = "" if "## Walkthroughs" in note.body else "## Walkthroughs\n"
            app.vault.write(scope, f"{section}- [[{link}]] ({state}, {local_date(app)})", op="append")
            app.vault.write_status(scope, conv["id"], {"state": "done" if state == "done" else "blocked", "doing": "", "next": "", "done": summary})
        except Exception as exc:
            return ToolResult.error(f"could not write the walkthrough: {exc}")
    ctx.extras["finish"] = {"state": state, "summary": summary, "walkthrough_path": note_path}
    ctx.extras["status_written"] = True
    return ToolResult(f"finished ({state}); walkthrough at {note_path or '(no scope note)'}. This turn ends now.")


STATUS_FIELDS = ("state", "goal", "done", "doing", "next", "blockers")


async def _status_update(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    app = ctx.app
    conv = ctx.conversation
    scope = conv.get("scope_note")
    if not scope:
        return ToolResult.error("this conversation has no scope note; status_update applies to threads with a project note")
    fields = {k: (str(v) if not isinstance(v, list) else "\n".join(f"- {x}" for x in v)) for k, v in args.items() if k in STATUS_FIELDS and v is not None}
    if not fields:
        return ToolResult.error("nothing to update; pass at least one of " + ", ".join(STATUS_FIELDS))
    if "state" in fields and fields["state"] not in ("planning", "working", "blocked", "waiting", "done"):
        return ToolResult.error("state must be one of planning, working, blocked, waiting, done")
    try:
        app.vault.write_status(scope, conv["id"], fields)
    except Exception as exc:
        return ToolResult.error(f"status write failed: {exc}")
    ctx.extras["status_written"] = True
    app.bus.emit("knowledge.changed", {"paths": [scope]}, conv["id"])
    return ToolResult("status block updated: " + ", ".join(fields))


async def _item_post(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    kind = args.get("kind") or "notification"
    if kind not in ("notification", "setup"):
        return ToolResult.error("kind must be notification or setup")
    row = items.post(
        ctx.app, kind, args.get("title", "").strip() or "(untitled)", args.get("body") or "",
        conversation_id=ctx.conversation_id, source=f"conversation:{ctx.conversation_id}", notify=bool(args.get("notify")),
    )
    return ToolResult(f"posted item {row['id']} to the now board")


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


def tools() -> list[Tool]:
    return [
        Tool(
            "conversation_create", prompts.load("tools/conversation_create"),
            _schema({
                "goal": {"type": "string"},
                "scope_note": {"type": "string", "description": "vault path of the project note"},
                "project_name": {"type": "string", "description": "name for a new project note when none exists"},
                "excerpt": {"type": "string"},
                "roots": {"type": "array", "items": {"type": "string"}},
                "preset": {"type": "string"},
                "title": {"type": "string"},
            }, ["goal"]),
            _create,
        ),
        Tool("conversation_send", prompts.load("tools/conversation_send"),
             _schema({"conversation_id": {"type": "string"}, "content": {"type": "string"}}, ["conversation_id", "content"]), _send),
        Tool("conversation_read", prompts.load("tools/conversation_read"),
             _schema({"conversation_id": {"type": "string"}, "limit": {"type": "integer"}, "before": {"type": "integer"}}, ["conversation_id"]), _read, read_only=True),
        Tool("conversation_list", prompts.load("tools/conversation_list"),
             _schema({"status": {"type": "string"}, "include_archived": {"type": "boolean"}}, []), _list, read_only=True),
        Tool("conversation_finish", prompts.load("tools/conversation_finish"),
             _schema({"summary": {"type": "string"}, "walkthrough": {"type": "string"}, "state": {"type": "string", "enum": ["done", "failed"]}}, ["summary", "walkthrough"]), _finish),
        Tool("status_update", prompts.load("tools/status_update"),
             _schema({k: {"type": "string"} for k in STATUS_FIELDS} | {"state": {"type": "string", "enum": ["planning", "working", "blocked", "waiting", "done"]}}, []), _status_update),
        Tool("item_post", prompts.load("tools/item_post"),
             _schema({"kind": {"type": "string", "enum": ["notification", "setup"]}, "title": {"type": "string"}, "body": {"type": "string"}, "notify": {"type": "boolean"}}, ["title"]), _item_post),
    ]
