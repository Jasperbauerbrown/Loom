"""Threads (03 §6.7) and the conversation-facing tools: conversation_create / send /
stop / read / list / finish, status_update, item_post. The surface contract exposed to the
model, so the desk spawns and talks to threads the same way a surface does.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import zoneinfo
from typing import Any

from .. import db, items, prompts
from ..ids import ulid
from ..tools.registry import Tool, ToolContext, ToolResult
from . import compaction, events, pins, status, stop

log = logging.getLogger(__name__)


class ThreadError(Exception):
    pass


def create_thread(  # type: ignore[no-untyped-def]
    app,
    *,
    goal: str,
    parent_id: str | None = None,
    roots: list[str] | None = None,
    preset: str | None = None,
    title: str | None = None,
    excerpt: str | None = None,
    notes: list[str] | None = None,
    role: str = "worker",
    created_by: str = "user",
    isolation: str | None = None,
    isolation_root: str | None = None,
    user_request: str | None = None,
) -> dict[str, Any]:
    """Persist the row, emit `conversation.created`, then post the goal (+ excerpt) to its
    inbox — the row exists before the first turn is scheduled. `notes` are vault paths
    pinned into the thread's context from its first turn: what the spawning conversation
    already checked before deciding to spawn. A missing note refuses the spawn.

    `isolation="worktree"` (03 §6.7, generalising §15.1) gives the thread its own git
    checkout of the repository holding `isolation_root` (default: its first root). The
    worktree *replaces* that root and leads the list: isolation means the thread cannot
    reach the live checkout it branched from, and relative paths resolve inside its own.

    `user_request` is the user's own typed message the spawning turn answered; a thread
    the user created themselves takes its goal, and a sub-thread with neither inherits
    its parent's. The permission judge reads it as the user's request for the thread's
    whole life (03 §9.5, 2026-10-19)."""
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
    try:
        pinned = pins.normalise(app, notes)
    except pins.PinError as exc:
        raise ThreadError(str(exc)) from exc
    roots = [_norm(r) for r in (roots or []) if r]
    from . import worktree as _worktree

    # Everything that can be judged without touching the disk is judged first, so a
    # refusal never leaves a row or a directory behind.
    isolation = (isolation or "none").strip().lower()
    if isolation not in ("none", "worktree"):
        raise ThreadError(f"isolation must be 'none' or 'worktree', not {isolation!r}")
    source_root: str | None = None
    if isolation == "worktree":
        if isolation_root:
            source_root = _norm(isolation_root)
            if not any(_worktree.same_path(source_root, r) for r in roots):
                raise ThreadError(
                    f"isolation_root {source_root} is not one of this thread's roots "
                    f"({', '.join(roots) if roots else 'none'}); pass the repository in roots too"
                )
        elif roots:
            source_root = roots[0]
        else:
            raise ThreadError("worktree isolation needs a root; pass the repository in roots")
        if _worktree.repo_root(source_root) is None:
            raise ThreadError(
                f"{source_root} is not inside a git repository; worktree isolation needs one — "
                "make it a repository with one commit, or spawn without isolation"
            )
    cid = ulid()
    now = db.now_iso()
    title = (title or "").strip() or default_title(goal)
    user_request = (user_request or "").strip() or (goal if created_by == "user" else "") or parent.get("user_request") or None
    with db.transaction(app.db):
        app.db.execute(
            "INSERT INTO conversations(id, kind, parent_id, title, goal, pinned_notes, roots, depth, role, preset, status, user_request, created_at, updated_at) "
            "VALUES (?, 'thread', ?, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?, ?)",
            (cid, parent_id, title, goal, db.dumps(pinned), db.dumps(roots), depth, role, preset, user_request, now, now),
        )
    if source_root is not None:
        # Persist before act: the row (worktree NULL) exists before git makes a checkout,
        # so a crash mid-`worktree add` leaves something we can see rather than an orphan
        # directory. The row is only completed once the checkout is really there.
        try:
            made = _worktree.create(app, cid, source_root)
        except _worktree.WorktreeError as exc:
            # Nothing has been emitted or posted yet, so nothing has seen this row.
            with db.transaction(app.db):
                app.db.execute("DELETE FROM conversations WHERE id=?", (cid,))
            raise ThreadError(str(exc)) from exc
        roots = [made["path"]] + [r for r in roots if not _worktree.same_path(r, source_root)]
        events.update(app, cid, worktree=made["path"], roots=roots, updated_at=db.now_iso())
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
    from ..tools import fs_guard

    return fs_guard.normalise(p)


TITLE_MAX = 80


def default_title(goal: str) -> str:
    """A thread's default title: the goal's first sentence, cut at a word boundary. It is
    a label — it lands on the now board, in notifications and in walkthrough note names —
    so it must not end mid-word ("…an Elegoo Centauri", 2026-09-21)."""
    first = next((line.strip() for line in goal.splitlines() if line.strip()), "")
    match = re.match(r"(.+?[.!?])(?=\s|$)", first)
    sentence = (match.group(1) if match else first).strip()
    if len(sentence) <= TITLE_MAX:
        return sentence.rstrip(".").strip() or first[:TITLE_MAX]
    cut = sentence[: TITLE_MAX + 1].rsplit(" ", 1)[0].rstrip(" ,;:-—")
    return (cut or sentence[:TITLE_MAX]) + "…"


def render_transcript(app, conv: dict[str, Any], limit: int = 30, before: int | None = None) -> str:  # type: ignore[no-untyped-def]
    evs = events.list_events(app, conv["id"], before=before, limit=limit, descending=True)
    evs.reverse()
    head = [
        f"conversation {conv['id']} — {conv.get('title') or ''}",
        f"kind: {conv['kind']}  status: {conv['status']}  updated: {conv.get('updated_at')}",
    ]
    if conv.get("goal"):
        head.append(f"goal: {conv['goal']}")
    pinned = pins.get(conv)
    if pinned:
        head.append("pinned notes: " + ", ".join(pinned))
    if conv.get("summary"):
        head.append(f"summary: {conv['summary'][:1500]}")
    body = compaction.render_for_summary([e for e in evs if e["kind"] != "thinking"])
    first_id = evs[0]["id"] if evs else None
    tail = f"\n[showing {len(evs)} events; conversation_read(before={first_id}) for earlier]" if first_id and len(evs) >= limit else ""
    return "\n".join(head) + "\n\n" + body + tail


# -- finish delivery (03 §6.7) -----------------------------------------------------------

WALKTHROUGH_DELIVERY_CHARS = 8_000
EVIDENCE_MAX_ITEMS = 40
EVIDENCE_CAPTION_CHARS = 140
_IMAGE_MARKER = re.compile(r"^\[image img_[0-9A-HJKMNP-TV-Z]{26} attached to this result\]$", re.MULTILINE)


def _caption(text: str, fallback: str = "") -> str:
    """The first line that says something, cut to a caption. Image markers and artifact
    pointers are the harness's own words, not the tool's, so they never become one."""
    for line in _IMAGE_MARKER.sub("", text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("[artifact "):
            return line[:EVIDENCE_CAPTION_CHARS]
    return fallback


def produced(app, conversation_id: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:  # type: ignore[no-untyped-def]
    """`(artifacts, images)` this conversation produced, each `(id, caption)`, oldest
    first. Artifacts come from their rows (the caption is the head's first line); images
    have no rows, so they come from the `meta.images` refs of the transcript events that
    carried them — a tool result, or a message the person attached them to."""
    store = getattr(app, "artifacts", None)
    artifacts: list[tuple[str, str]] = []
    for row in (store.list(conversation_id) if store is not None else []):
        artifacts.append((row["id"], _caption(row.get("head") or "", f"{row.get('bytes') or 0} bytes")))
    images: list[tuple[str, str]] = []
    seen: set[str] = set()
    for e in events.list_events(app, conversation_id, limit=1_000_000):
        refs = (e["meta"] or {}).get("images") or []
        if not refs:
            continue
        name = e["meta"].get("name") or ("attached by the person" if e["kind"] == "user" else e["kind"])
        caption = _caption(e.get("content") or "", name)
        for ref in refs:
            image_id = str(ref.get("id") if isinstance(ref, dict) else ref or "")
            if image_id and image_id not in seen:
                seen.add(image_id)
                images.append((image_id, f"{name}: {caption}" if caption != name else name))
    return artifacts, images


def evidence_block(app, conv: dict[str, Any], walkthrough_path: str, walkthrough: str) -> str:  # type: ignore[no-untyped-def]
    """What rides with the summary to the spawning conversation: the walkthrough section
    just filed (path and text), then every artifact and image the thread produced, as an
    id with a caption. The spawner's judgment is fed, not pointed at — the raylib thread
    cited a screenshot the desk had no way to open, and the desk spent 43 minutes
    re-deriving the work instead (tower-defense run, 2026-09-24)."""
    text = walkthrough.strip()
    if len(text) > WALKTHROUGH_DELIVERY_CHARS:
        text = (
            text[:WALKTHROUGH_DELIVERY_CHARS]
            + f"\n[… {len(text) - WALKTHROUGH_DELIVERY_CHARS} more chars; knowledge_read(\"{walkthrough_path}\") for the rest]"
        )
    parts = [prompts.load("context/finish-evidence"), f"# Walkthrough — {walkthrough_path}\n{text}"]
    artifacts, images = produced(app, conv["id"])
    for label, rows in (("Artifacts", artifacts), ("Images", images)):
        if not rows:
            continue
        shown = rows[:EVIDENCE_MAX_ITEMS]
        lines = [f"# {label}"] + [f"- {i} — {c}" if c else f"- {i}" for i, c in shown]
        if len(rows) > len(shown):
            lines.append(f"[{len(rows) - len(shown)} more; the thread's transcript has them]")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


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
            roots=args.get("roots") or [],
            preset=args.get("preset"),
            title=args.get("title"),
            excerpt=args.get("excerpt"),
            notes=args.get("notes") or [],
            created_by=f"conversation:{ctx.conversation_id}",
            isolation=args.get("isolation"),
            isolation_root=args.get("isolation_root"),
            user_request=ctx.request_text if ctx.request_from_user else None,
        )
    except ThreadError as exc:
        return ToolResult.error(str(exc))
    pinned = pins.get(row)
    isolated = ""
    if row.get("worktree"):
        from . import worktree as _worktree

        isolated = f" It works in an isolated checkout at {row['worktree']} on branch {_worktree.branch_for(row['id'])}."
    return ToolResult(
        f"created thread {row['id']} (\"{row['title']}\"); pinned: {', '.join(pinned) if pinned else 'none'}.{isolated} "
        "It will report back when it stops without finishing, is blocked, or finishes."
    )


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


async def _stop(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    app = ctx.app
    target = args.get("conversation_id", "")
    other = app.conversation(target)
    if other is None:
        return ToolResult.error(f"conversation {target} not found")
    if not stop.is_descendant(app, ctx.conversation_id, target):
        return ToolResult.error(f"conversation {target} is not one of your threads; you can only stop threads you spawned, or their sub-threads")
    stopped = stop.stop(app, target, actor=f"conversation {ctx.conversation_id}", reason=args.get("reason") or "")
    if not stopped:
        return ToolResult(f"nothing to stop: {target} is {other['status']} with no work in flight")
    names = ", ".join(f"{s['id']} (\"{s['title']}\", was {s['was']})" for s in stopped)
    return ToolResult(f"stopped {len(stopped)}: {names}. Worktrees are kept; a later conversation_send reopens a stopped thread.")


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
    sql = "SELECT id, kind, parent_id, title, goal, pinned_notes, status, depth, preset, updated_at FROM conversations"
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
    lines = ["id | kind | status | title | goal | pinned | parent | preset | updated"]
    for r in rows:
        lines.append(
            f"{r['id']} | {r['kind']} | {r['status']} | {r['title'] or ''} | {(r['goal'] or '')[:80]} | "
            f"{', '.join(pins.get(dict(r)))} | {r['parent_id'] or ''} | {r['preset'] or ''} | {r['updated_at']}"
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
    date = local_date(app)
    title = conv.get("title") or conv["id"]
    try:
        # A section of the daily log, so the evidence outlives the transcript. Every
        # thread files here (03 §6.7, decided 2026-09-22): a project note, when the work
        # has one, links to the section as ordinary knowledge.
        note_path = f"log/{date}.md"
        section = f"## {title} ({conv['id']})\nstate: {state}\n\n{walkthrough}\n"
        # The daily log's name is its date, so "today" in it is absolute by construction;
        # the vault's relative-date refusal is for notes that carry no date. Four
        # finishes in one night each cost a retry over the word "today" (2026-09-29).
        if app.vault.exists(note_path):
            app.vault.write(note_path, section, op="append", check_dates=False)
        else:
            app.vault.write(
                note_path, f"# {date}\n\n{section}", op="create",
                frontmatter={"type": "log", "title": date}, check_dates=False,
            )
        status.write(app, conv, {"state": "done" if state == "done" else "blocked", "doing": "", "next": "", "done": summary}, owner=conv["id"])
    except Exception as exc:
        return ToolResult.error(f"could not write the walkthrough: {exc}")
    # The spawner gets the summary first and the evidence under it, in one message
    # (03 §6.7): `delivery` is the inbox body, `summary` stays the one-line now-board
    # item. Built here, where the walkthrough text is, so the runner only delivers it.
    try:
        delivery = f"{summary}\n\n{evidence_block(app, conv, note_path, walkthrough)}"
    except Exception as exc:  # evidence is an aid, never the reason a finish fails
        log.warning("finish evidence for %s failed: %r", conv["id"], exc)
        delivery = summary
    ctx.extras["finish"] = {"state": state, "summary": summary, "walkthrough_path": note_path, "delivery": delivery}
    ctx.extras["status_written"] = True
    return ToolResult(f"finished ({state}); walkthrough at {note_path}. This turn ends now.")


STATUS_FIELDS = status.FIELDS


async def _status_update(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    app = ctx.app
    conv = ctx.conversation
    if conv["kind"] != "thread":
        return ToolResult.error("status_update applies to threads; the desk publishes no status")
    fields = {k: (str(v) if not isinstance(v, list) else "\n".join(f"- {x}" for x in v)) for k, v in args.items() if k in STATUS_FIELDS and v is not None}
    if not fields:
        return ToolResult.error("nothing to update; pass at least one of " + ", ".join(STATUS_FIELDS))
    if "state" in fields and fields["state"] not in status.STATES:
        return ToolResult.error("state must be one of " + ", ".join(status.STATES))
    try:
        sink = status.write(app, conv, fields, owner=conv["id"])
    except Exception as exc:
        return ToolResult.error(f"status write failed: {exc}")
    ctx.extras["status_written"] = True
    where = "status block updated" if sink == "note" else "status recorded"
    return ToolResult(f"{where}: " + ", ".join(fields))


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
                "excerpt": {"type": "string"},
                "roots": {"type": "array", "items": {"type": "string"}},
                "preset": {"type": "string"},
                "title": {"type": "string"},
                "notes": {"type": "array", "items": {"type": "string"}, "description": "vault paths to pin into the thread's context every turn (the notes you checked before spawning); a missing note refuses the spawn"},
                "isolation": {"type": "string", "enum": ["none", "worktree"], "description": "\"worktree\" gives the thread its own git checkout of the repository it works in, on branch loom/<thread id>; it cannot touch the live checkout"},
                "isolation_root": {"type": "string", "description": "which of `roots` the worktree is made from; defaults to the first root"},
            }, ["goal"]),
            _create,
        ),
        Tool("conversation_send", prompts.load("tools/conversation_send"),
             _schema({"conversation_id": {"type": "string"}, "content": {"type": "string"}}, ["conversation_id", "content"]), _send),
        Tool("conversation_stop", prompts.load("tools/conversation_stop"),
             _schema({"conversation_id": {"type": "string"}, "reason": {"type": "string", "description": "short; recorded in the stopped thread's transcript"}}, ["conversation_id"]), _stop),
        Tool("conversation_read", prompts.load("tools/conversation_read"),
             _schema({"conversation_id": {"type": "string"}, "limit": {"type": "integer"}, "before": {"type": "integer"}}, ["conversation_id"]), _read, read_only=True),
        Tool("conversation_list", prompts.load("tools/conversation_list"),
             _schema({"status": {"type": "string"}, "include_archived": {"type": "boolean"}}, []), _list, read_only=True),
        Tool("conversation_finish", prompts.load("tools/conversation_finish"),
             _schema({"summary": {"type": "string"}, "walkthrough": {"type": "string"}, "state": {"type": "string", "enum": ["done", "failed"]}}, ["summary", "walkthrough"]), _finish,
             kinds=("thread",)),
        Tool("status_update", prompts.load("tools/status_update"),
             _schema({k: {"type": "string"} for k in STATUS_FIELDS} | {"state": {"type": "string", "enum": list(status.STATES)}}, []), _status_update,
             kinds=("thread",)),
        Tool("item_post", prompts.load("tools/item_post"),
             _schema({"kind": {"type": "string", "enum": ["notification", "setup"]}, "title": {"type": "string"}, "body": {"type": "string"}, "notify": {"type": "boolean"}}, ["title"]), _item_post),
    ]
