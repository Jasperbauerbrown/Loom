"""`assemble` — the only context builder (03 §7).

Message layout sent to the model:

    [0] system  slots [1]–[4]: soul + kind prompt, tools line, core blocks, procedures
                index. Byte-stable across turns (sorted, no dates) for the prompt cache.
    [1] system  slots [5]–[7]: now board (desk) / project note (thread), now line +
                steps remaining, summary. Rebuilt every step.
    ...        slot [8]: live events replayed since `summary_cutoff`, evictions applied.
    user       slot [9]: <knowledge may_be_irrelevant="true"> pre-loaded entities, spliced
                immediately before this turn's first user message. Ephemeral.
    user…      slot [10]: this turn's claimed inbox rows, then whatever the runner appends
                (assistant steps with in-turn reasoning, tool results, mid-turn inbox).
"""

from __future__ import annotations

import datetime as _dt
import logging
import zoneinfo
from dataclasses import dataclass, field
from typing import Any

from .. import db, items, prompts
from ..tools.registry import Tool
from . import events, inbox, status

log = logging.getLogger(__name__)

KNOWLEDGE_OPEN = '<knowledge may_be_irrelevant="true">'
KNOWLEDGE_CLOSE = "</knowledge>"


# -- images ------------------------------------------------------------------------
# Transcript rows carry only `{"id", "mime"}` refs (03 §4.4); the data URLs the model
# sees are built here, at assembly time, and live only in memory.

def image_urls(app, conversation_id: str, refs: list[Any] | None) -> list[str]:  # type: ignore[no-untyped-def]
    """Refs → data URLs. A missing file (pruned, deleted by hand) is skipped with a log
    line: an absent image must never break context assembly."""
    urls: list[str] = []
    store = getattr(app, "artifacts", None)
    for ref in refs or []:
        image_id = str(ref.get("id") or "") if isinstance(ref, dict) else str(ref or "")
        url = store.image_data_url(conversation_id, image_id) if store is not None and image_id else None
        if url is None:
            log.warning("conversation %s: image %s referenced by the transcript is missing — skipped", conversation_id, image_id)
            continue
        urls.append(url)
    return urls


def tool_image_followup(app, conversation_id: str, call_id: str, refs: list[Any] | None) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    """The user-role message that carries a tool result's images. Tool-role messages
    cannot carry image parts on most providers, so the result stays text (with a marker
    per image) and this follower is appended after the whole step's tool messages — the
    same shape in replay and in-turn. Never persisted: the result's `meta.images` is the
    record."""
    urls = image_urls(app, conversation_id, refs)
    if not urls:
        return None
    note = prompts.load("context/tool-image-note").replace("{call_id}", str(call_id or ""))
    return {"role": "user", "content": note, "images": urls}


def image_tokens_for(app, conv: dict[str, Any]) -> int:  # type: ignore[no-untyped-def]
    """The per-image prompt-token estimate of the conversation's preset."""
    from ..models import roles

    try:
        return roles.image_tokens(roles.resolve(app, role=conv["role"], preset=conv.get("preset")).params)
    except Exception:
        return roles.DEFAULT_IMAGE_TOKENS


@dataclass
class Context:
    conv: dict[str, Any]
    prefix: list[dict[str, Any]]  # one system message, stable
    history: list[dict[str, Any]]  # slot [8], events before this turn
    turn: list[dict[str, Any]]  # slot [9] + [10] + in-turn appends
    resolved_paths: list[str]
    knowledge_text: str
    exposed: set[str] = field(default_factory=set)  # tool names loaded via tool_search this turn
    turn_start_event_id: int | None = None

    def messages(self, app, steps_remaining: int | None = None) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
        """One system message (stable prefix), then the summary as a user-role context
        block, the live history, the volatile context block (now board / scope note, now
        line, steps) nearest the query, then this turn's messages. Exactly one system
        message: chat templates (Qwen 3.x among them) reject a system message that is
        not first, and everything volatile sitting after the history keeps the cached
        prefix as long as possible (decided 2026-09-20)."""
        out = [*self.prefix]
        if self.conv.get("summary"):
            out.append(summary_message(self.conv))
        out.extend(self.history)
        out.append(volatile_message(app, self.conv, steps_remaining))
        out.extend(self.turn)
        return out

    def tools(self, app, enabled: bool = True) -> list[Tool]:  # type: ignore[no-untyped-def]
        if not enabled:
            return []
        limit = int(app.settings.get("conversation.tool_expose_max"))
        return app.tools.expose(limit, self.exposed)

    def tool_schemas(self, app, enabled: bool = True) -> list[dict[str, Any]] | None:  # type: ignore[no-untyped-def]
        tools = self.tools(app, enabled)
        return [t.to_openai() for t in tools] if tools else None

    def chars(self, app, steps_remaining: int | None = None) -> int:  # type: ignore[no-untyped-def]
        per_image = image_tokens_for(app, self.conv) * 4
        n = 0
        for m in self.messages(app, steps_remaining):
            c = m.get("content")
            n += len(c) if isinstance(c, str) else 0
            for tc in m.get("tool_calls") or []:
                n += len(db.dumps(tc))
            n += len(m.get("images") or []) * per_image
        schemas = self.tool_schemas(app)
        if schemas:
            n += len(db.dumps(schemas))
        return n


# -- slots [1]–[4] -----------------------------------------------------------------

def stable_prefix(app, kind: str) -> str:  # type: ignore[no-untyped-def]
    parts = [prompts.load("system"), prompts.load("kind-desk" if kind == "desk" else "kind-thread")]
    parts.append("Tools are provided via the API; use `tool_search` for more.")
    core = app.index.core_blocks(int(app.settings.get("knowledge.core_block_chars")))
    if core:
        blocks = [f"## {path}\n{text}" for path, text in core]
        parts.append("# Core notes\n\n" + "\n\n".join(blocks))
    procedures = app.index.procedures_index()
    if procedures:
        parts.append("# Procedures (procedure_load <name> for the full text)\n" + procedures)
    return "\n\n".join(p for p in parts if p)


# -- slots [5]–[7] -----------------------------------------------------------------

def now_line(app) -> str:  # type: ignore[no-untyped-def]
    tz_name = app.settings.get("general.timezone") or "UTC"
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = _dt.timezone.utc
        tz_name = "UTC"
    now = _dt.datetime.now(tz)
    return f"now: {now.strftime('%Y-%m-%d %H:%M')} ({tz_name}, {now.strftime('%A')})"


def now_board(app) -> str:  # type: ignore[no-untyped-def]
    """Desk slot [5]: pending approvals, unseen items, running threads (capped)."""
    lines: list[str] = []
    approvals = app.db.execute(
        "SELECT a.id, a.tool, a.conversation_id, c.title FROM approvals a JOIN conversations c ON c.id=a.conversation_id "
        "WHERE a.status='pending' ORDER BY a.created_at"
    ).fetchall()
    for a in approvals:
        lines.append(f'- approval pending: {a["tool"]} in "{a["title"] or a["conversation_id"]}" ({a["conversation_id"]})')
    for it in items.unseen(app):
        lines.append(f"- {it['kind']}: {it['title']}" + (f" — {it['body'][:120]}" if it.get("body") else ""))
    cap = int(app.settings.get("conversation.now_board_max_threads"))
    threads = app.db.execute(
        "SELECT id, title, goal, status, scope_note FROM conversations WHERE kind='thread' "
        "AND status IN ('running','waiting_approval','waiting_input') ORDER BY updated_at DESC LIMIT ?",
        (cap,),
    ).fetchall()
    for t in threads:
        doing = ""
        try:
            fields, _owner, _sink = status.read(app, dict(t))
            if fields:
                doing = fields.get("doing") or fields.get("state") or ""
        except Exception:
            doing = ""
        label = t["title"] or t["goal"][:60]
        lines.append(f'- thread "{label}" ({t["id"]}): {t["status"]}' + (f" — {doing[:120]}" if doing else ""))
    if not lines:
        return "# Now board\n(empty)"
    return "# Now board\n" + "\n".join(lines)


def scope_block(app, conv: dict[str, Any]) -> str:  # type: ignore[no-untyped-def]
    path = conv.get("scope_note")
    if not path:
        # A task: no project note; its status lives in its own transcript (03 §6.7).
        try:
            fields, _owner, _sink = status.read(app, conv)
        except Exception:
            fields = None
        return f"# Scope\ngoal: {conv.get('goal') or ''}\n\n# Status\n{status.render(fields)}"
    try:
        note = app.vault.read(path)
        body = note.body if note.error is None else note.raw
    except Exception as exc:  # missing note is an expected input
        return f"# Scope note {path}\n(unreadable: {exc})\ngoal: {conv.get('goal') or ''}"
    return f"# Scope note {path}\n{body.strip()}"


CONTEXT_OPEN = '<context from="harness">'
CONTEXT_CLOSE = "</context>"


def filesystem_block(app, conv: dict[str, Any]) -> str:  # type: ignore[no-untyped-def]
    """Slot [6]: the roots this conversation may touch and the paths that always ask.
    Until 2026-09-21 the model learned its roots only by failing against them (a desk
    guessed `projects/`, listed the artifacts folder, tried a Linux path, then asked for
    the home directory); mutable state belongs here, not in the soul."""
    from ..tools import fs_guard

    paths = fs_guard.describe(app, conv)
    lines = ["# Filesystem"]
    roots = paths.get("roots") or []
    if roots:
        lines.append("roots (relative paths resolve against the first): " + ", ".join(roots))
    else:
        lines.append("roots: (none — every file access will ask)")
    protected = paths.get("protected") or []
    if protected:
        lines.append("protected (always ask): " + ", ".join(protected))
    return "\n".join(lines)


def volatile_message(app, conv: dict[str, Any], steps_remaining: int | None = None) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Slots [5]–[6] as one user-role block, fenced so the model can tell harness
    context from the person's words."""
    parts = [now_board(app) if conv["kind"] == "desk" else scope_block(app, conv), filesystem_block(app, conv)]
    line = now_line(app)
    if conv["kind"] == "thread" and steps_remaining is not None:
        line += f"\n[steps remaining: {steps_remaining}]"
    parts.append(line)
    return {"role": "user", "content": f"{CONTEXT_OPEN}\n" + "\n\n".join(parts) + f"\n{CONTEXT_CLOSE}"}


def summary_message(conv: dict[str, Any]) -> dict[str, Any]:
    """Slot [7]: the compaction summary, placed right after the stable prefix."""
    return {"role": "user", "content": f"{CONTEXT_OPEN}\n# Summary of earlier conversation\n{conv['summary']}\n{CONTEXT_CLOSE}"}


# -- slot [8] -------------------------------------------------------------------

def replay(app, conv: dict[str, Any], before_event_id: int | None = None) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """Live events since `summary_cutoff` as chat messages, evictions applied; each
    message carries `_event_id` (stripped before sending)."""
    cutoff = int(conv.get("summary_cutoff") or 0)
    evs = events.list_events(app, conv["id"], after=cutoff, before=before_event_id, limit=1_000_000)
    evicted: dict[int, str | None] = {}
    for e in evs:
        if e["kind"] == "compaction" and e["meta"].get("kind") == "evict":
            arts = e["meta"].get("artifacts") or {}
            for i in e["meta"].get("event_ids", []):
                evicted[int(i)] = arts.get(str(i))
    return messages_from_events(app, conv["id"], evs, evicted)


def messages_from_events(  # type: ignore[no-untyped-def]
    app, conversation_id: str, evs: list[dict[str, Any]], evicted: dict[int, str | None] | None = None
) -> list[dict[str, Any]]:
    """`evicted` maps tool_result event id → artifact id (or None) for stubbed results.
    Image-bearing tool results queue a follower message that is flushed after the step's
    last tool message (before the next event of any other kind); evicted results lost
    their images with their content, so they get none."""
    evicted = evicted or {}
    msgs: list[dict[str, Any]] = []
    open_calls: set[str] = set()
    followers: list[dict[str, Any]] = []

    def flush() -> None:
        msgs.extend(followers)
        followers.clear()

    for e in evs:
        kind = e["kind"]
        if kind == "tool_result":
            call_id = e["meta"].get("call_id", "")
            if e["id"] in evicted:
                art = evicted[e["id"]] or e["meta"].get("artifact_id")
                content = f"[evicted; artifact_recall({art})]" if art else "[evicted]"
            else:
                content = e["content"]
                if e["meta"].get("images"):
                    follower = tool_image_followup(app, conversation_id, call_id, e["meta"]["images"])
                    if follower is not None:
                        follower["_event_id"] = e["id"]
                        followers.append(follower)
            msgs.append({"role": "tool", "tool_call_id": call_id, "content": content, "_event_id": e["id"]})
            open_calls.discard(call_id)
            continue
        flush()
        if kind == "user":
            msg: dict[str, Any] = {"role": "user", "content": e["content"], "_event_id": e["id"]}
            urls = image_urls(app, conversation_id, e["meta"].get("images"))
            if urls:
                msg["images"] = urls
            msgs.append(msg)
        elif kind == "assistant":
            msgs.append({"role": "assistant", "content": e["content"], "_event_id": e["id"]})
        elif kind == "tool_call":
            calls = db.loads(e["content"], [])
            msgs.append(
                {
                    "role": "assistant",
                    "content": e["meta"].get("text") or "",
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": db.dumps(c.get("arguments", {}))},
                        }
                        for c in calls
                    ],
                    "_event_id": e["id"],
                }
            )
            open_calls = {c["id"] for c in calls}
        # thinking / system / status / compaction: never replayed
    flush()
    return msgs


def strip_private(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]


# -- slot [9] -------------------------------------------------------------------

async def resolve_knowledge(app, conv: dict[str, Any], text: str) -> tuple[list[str], str]:  # type: ignore[no-untyped-def]
    """Slot [9] inputs: resolved entity paths and the rendered pre-load block. Async
    because the embedding fill-up is a model call; failures leave the slot empty."""
    if not text.strip():
        return [], ""
    exclude = {conv["scope_note"]} if conv.get("scope_note") else set()
    try:
        paths = await app.index.resolve(text, exclude=exclude)
    except Exception:
        paths = []
    if not paths:
        return [], ""
    budget = int(app.settings.get("knowledge.preload_budget_tokens"))
    body = app.index.preload(paths, budget)
    if not body.strip():
        return paths, ""
    return paths, f"{KNOWLEDGE_OPEN}\n{body.strip()}\n{KNOWLEDGE_CLOSE}"


# -- the builder ------------------------------------------------------------------

def assemble(  # type: ignore[no-untyped-def]
    app,
    conv: dict[str, Any],
    claimed: list[dict[str, Any]],
    turn_start_event_id: int | None,
    *,
    resolved: tuple[list[str], str] | None = None,
) -> Context:
    """Build the context for a turn. `claimed` rows have already been persisted as user
    events with ids ≥ `turn_start_event_id`; history replays everything before that and
    `turn` holds the knowledge block + this turn's user messages. On a mid-turn rebuild
    (after compaction) pass `turn_start_event_id` again and the previously resolved
    knowledge so the splice lands in the same place."""
    prefix = [{"role": "system", "content": stable_prefix(app, conv["kind"])}]
    all_msgs = replay(app, conv)
    if turn_start_event_id is None:
        history, turn_msgs = all_msgs, []
    else:
        history = [m for m in all_msgs if m["_event_id"] < turn_start_event_id]
        turn_msgs = [m for m in all_msgs if m["_event_id"] >= turn_start_event_id]
    paths, knowledge_text = resolved if resolved is not None else ([], "")
    turn: list[dict[str, Any]] = []
    if knowledge_text:
        turn.append({"role": "user", "content": knowledge_text, "_ephemeral": True})
    turn.extend(turn_msgs)
    return Context(
        conv=conv,
        prefix=prefix,
        history=history,
        turn=turn,
        resolved_paths=paths,
        knowledge_text=knowledge_text,
        turn_start_event_id=turn_start_event_id,
    )


def occupancy(app, conv: dict[str, Any], ctx: Context | None = None) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Estimated prompt tokens vs the compaction limit (03 §6.5)."""
    from ..models import roles

    try:
        resolved = roles.resolve(app, role=conv["role"], preset=conv.get("preset"))
        params = resolved.params
    except Exception:
        params = {}
    effective_ctx = int(params.get("effective_ctx") or 32768)
    max_tokens = int(params.get("max_tokens") or 4096)
    threshold = float(app.settings.get("conversation.compact_threshold"))
    limit = int(min(effective_ctx * threshold, effective_ctx - min(max_tokens, effective_ctx / 2)))
    factor = float(conv.get("token_factor") or 1.0)
    if ctx is None:
        ctx = assemble(app, conv, [], None)
    chars = ctx.chars(app)
    tokens = int(chars / 4 * factor)
    return {
        "occupancy_tokens": tokens,
        "limit_tokens": limit,
        "effective_ctx": effective_ctx,
        "max_tokens": max_tokens,
        "token_factor": factor,
        "chars": chars,
        "summary_chars": len(conv.get("summary") or ""),
    }
