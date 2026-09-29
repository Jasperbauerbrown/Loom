"""Compaction ladder (03 §6.5) and calibration (§6.6).

    occupancy = chars(messages + tools) / 4 × token_factor
    limit     = min(effective_ctx × compact_threshold, effective_ctx − min(max_tokens, effective_ctx/2))

Ladder when occupancy ≥ limit: 1 evict (reversible: old tool results → stubs, spilled to
artifacts), 2 fold (lossy: oldest 70% of live events → summary via the maintenance role
and prompts/summarize.md), 3 fail loudly (two folds without reduction → CompactionError).
Transcript rows are never mutated: eviction and folding are recorded as `compaction`
events and applied at read time by `context.replay`.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .. import db, prompts
from ..models import roles
from ..models.types import PRIORITY_MAINTENANCE
from . import events
from .context import Context

log = logging.getLogger(__name__)

EVICT_MIN_CHARS = 200  # evicting shorter results gains nothing
THINK_EXCERPT = 500


class CompactionError(Exception):
    pass


def limits(app, conv: dict[str, Any]) -> tuple[int, int, int]:  # type: ignore[no-untyped-def]
    """(effective_ctx, max_tokens, limit_tokens)."""
    try:
        params = roles.resolve(app, role=conv["role"], preset=conv.get("preset")).params
    except Exception:
        params = {}
    effective_ctx = int(params.get("effective_ctx") or 32768)
    max_tokens = int(params.get("max_tokens") or 4096)
    threshold = float(app.settings.get("conversation.compact_threshold"))
    limit = int(min(effective_ctx * threshold, effective_ctx - min(max_tokens, effective_ctx / 2)))
    return effective_ctx, max_tokens, limit


def estimate(app, conv: dict[str, Any], ctx: Context, steps_remaining: int | None = None) -> int:  # type: ignore[no-untyped-def]
    factor = float(conv.get("token_factor") or 1.0)
    return int(ctx.chars(app, steps_remaining) / 4 * factor)


def live_events(app, conv: dict[str, Any]) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    return events.list_events(app, conv["id"], after=int(conv.get("summary_cutoff") or 0), limit=1_000_000)


def evicted_map(evs: list[dict[str, Any]]) -> dict[int, str | None]:
    out: dict[int, str | None] = {}
    for e in evs:
        if e["kind"] == "compaction" and e["meta"].get("kind") == "evict":
            arts = e["meta"].get("artifacts") or {}
            for i in e["meta"].get("event_ids", []):
                out[int(i)] = arts.get(str(i))
    return out


def evict(app, conv: dict[str, Any]) -> bool:  # type: ignore[no-untyped-def]
    """Step 1. Returns True if any result was evicted."""
    evs = [e for e in live_events(app, conv) if e["kind"] in events.REPLAY_KINDS]
    if not evs:
        return False
    already = evicted_map(live_events(app, conv))
    keep_from = int(len(evs) * 0.7)
    victims = [
        e
        for e in evs[:keep_from]
        if e["kind"] == "tool_result" and e["id"] not in already and len(e["content"]) >= EVICT_MIN_CHARS
    ]
    if not victims:
        return False
    artifacts: dict[str, str | None] = {}
    for e in victims:
        art = e["meta"].get("artifact_id")
        if not art:
            try:
                row = app.artifacts.spill(conv["id"], e["id"], e["content"])
                art = row["id"]
            except Exception:
                log.exception("spill during eviction failed for event %s", e["id"])
                art = None
        artifacts[str(e["id"])] = art
    ev_id = events.append(
        app,
        conv["id"],
        "compaction",
        f"evicted {len(victims)} tool results",
        {"kind": "evict", "event_ids": [e["id"] for e in victims], "artifacts": artifacts},
    )
    app.bus.emit("event.appended", events.get(app, ev_id), conv["id"])
    return True


def _image_note(e: dict[str, Any]) -> str:
    n = len((e.get("meta") or {}).get("images") or [])
    return f" [+{n} image{'s' if n != 1 else ''}]" if n else ""


VERDICT_REASON_CHARS = 120


def _verdict(meta: dict[str, Any]) -> str:
    """`; ask → allow: <reason>` when the harness did not simply allow the call — what
    the person was asked, what the classifier judged, what the desk auto-approved. A
    self-review reads permission friction off these; a plain `allow` says nothing."""
    policy = str(meta.get("policy") or "")
    if not policy or policy == "allow":
        return ""
    out = f"; {policy} → {meta.get('decision') or '?'}"
    reason = str(meta.get("reason") or "").replace("\n", " ").strip()
    if reason:
        out += f": {reason[:VERDICT_REASON_CHARS]}"
    return out


def render_for_summary(evs: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for e in evs:
        k = e["kind"]
        if k == "user":
            lines.append(f"USER: {e['content']}{_image_note(e)}")
        elif k == "assistant":
            lines.append(f"ASSISTANT: {e['content']}")
        elif k == "thinking":
            lines.append(f"ASSISTANT (reasoning excerpt): {e['content'][:THINK_EXCERPT]}")
        elif k == "tool_call":
            calls = db.loads(e["content"], [])
            for c in calls:
                args = db.dumps(c.get("arguments", {}))
                lines.append(f"TOOL CALL {c['name']}({args[:300]})")
            if e["meta"].get("text"):
                lines.append(f"ASSISTANT: {e['meta']['text']}")
        elif k == "tool_result":
            status = "ok" if e["meta"].get("ok", True) else "ERROR"
            preview = e["content"][:160].replace("\n", " ")
            lines.append(f"TOOL RESULT ({status}{_verdict(e['meta'])}): {preview}{_image_note(e)}")
        elif k == "system":
            lines.append(f"SYSTEM: {e['content']}")
    return "\n".join(lines)


async def fold(app, conv: dict[str, Any], priority: int = PRIORITY_MAINTENANCE) -> bool:  # type: ignore[no-untyped-def]
    """Step 2. Summarise the oldest 70% of live events into `summary`."""
    evs = [e for e in live_events(app, conv) if e["kind"] != "compaction"]
    replayable = [e for e in evs if e["kind"] in events.REPLAY_KINDS]
    if len(replayable) < 2:
        return False
    boundary = max(1, int(len(replayable) * 0.7))
    # never split a tool_call/tool_result group: advance past trailing results
    while boundary < len(replayable) and replayable[boundary]["kind"] == "tool_result":
        boundary += 1
    if boundary >= len(replayable):
        boundary = len(replayable) - 1
        while boundary > 0 and replayable[boundary]["kind"] == "tool_result":
            boundary -= 1
        if boundary <= 0:
            return False
    folded_through = replayable[boundary - 1]["id"]
    folded = [e for e in evs if e["id"] <= folded_through]
    previous = conv.get("summary") or ""
    user_text = ""
    if previous:
        user_text += f"# Previous summary\n{previous}\n\n"
    user_text += f"# Events being folded\n{render_for_summary(folded)}"
    request = roles.build_request(
        app,
        role="maintenance",
        messages=[{"role": "system", "content": prompts.load("summarize")}, {"role": "user", "content": user_text}],
        conversation_id=conv["id"],
        purpose="summarize",
    )
    result = await app.models.chat(request, priority)
    if not result.ok or not result.content.strip():
        raise CompactionError(f"summariser failed: {result.outcome} {result.error}".strip())
    summary = result.content.strip()
    events.update(app, conv["id"], summary=summary, summary_cutoff=folded_through)
    conv["summary"] = summary
    conv["summary_cutoff"] = folded_through
    ev_id = events.append(
        app,
        conv["id"],
        "compaction",
        f"folded {len(folded)} events into summary",
        {"kind": "fold", "folded_through": folded_through, "kept_from": folded_through + 1, "folded_count": len(folded), "summary_chars": len(summary)},
    )
    app.bus.emit("event.appended", events.get(app, ev_id), conv["id"])
    events.announce(app, conv["id"])
    return True


async def maybe_compact(  # type: ignore[no-untyped-def]
    app,
    conv: dict[str, Any],
    rebuild: Callable[[], Context],
    *,
    priority: int = PRIORITY_MAINTENANCE,
    force: bool = False,
    steps_remaining: int | None = None,
) -> Context | None:
    """Run the ladder if needed (or `force`). Returns the rebuilt Context when anything
    changed, else None. Raises CompactionError when two folds fail to reduce."""
    _ctx_, _mt_, limit = limits(app, conv)
    ctx = rebuild()
    occ = estimate(app, conv, ctx, steps_remaining)
    if occ < limit and not force:
        return None
    changed = False
    if evict(app, conv):
        changed = True
        ctx = rebuild()
        occ = estimate(app, conv, ctx, steps_remaining)
        if occ < limit and not force:
            return ctx
    stalls = 0
    for _ in range(3):
        before = occ
        if not await fold(app, conv, priority):
            break
        changed = True
        ctx = rebuild()
        occ = estimate(app, conv, ctx, steps_remaining)
        if occ < limit:
            return ctx
        if occ >= before * 0.95:
            stalls += 1
            if stalls >= 2:
                raise CompactionError("compaction is not reducing context; stopping to avoid thrash")
    if occ >= limit and force:
        raise CompactionError(f"context still over limit after compaction ({occ} ≥ {limit} tokens)")
    return ctx if changed else None


def calibrate(app, conv: dict[str, Any], prompt_tokens: int | None, prompt_chars: int) -> float | None:  # type: ignore[no-untyped-def]
    """03 §6.6: factor = prompt_tokens / (chars/4), clamped [0.5, 4]; a saturated reading
    can only raise it. Stored on the conversation and written back to the preset."""
    if not prompt_tokens or prompt_chars <= 0:
        return None
    effective_ctx, _mt, _lim = limits(app, conv)
    new = max(0.5, min(4.0, prompt_tokens / (prompt_chars / 4)))
    old = float(conv.get("token_factor") or 1.0)
    if prompt_tokens >= 0.98 * effective_ctx:
        new = max(old, new)
    events.update(app, conv["id"], token_factor=new)
    conv["token_factor"] = new
    try:
        resolved = roles.resolve(app, role=conv["role"], preset=conv.get("preset"))
        roles.update_token_factor_seed(app, resolved.preset["name"], new)
    except Exception:
        pass
    return new


def clear(app, conversation_id: str, actor: str = "user") -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Start the conversation's context fresh (decided 2026-09-21): the next turn replays
    nothing before this point. Transcript rows are untouched — they stay visible in the
    UI below a divider and searchable with transcript_search — so this is a cut, not a
    delete: a `compaction` event of kind `clear` with an empty summary."""
    conv = app.conversation(conversation_id)
    if conv is None:
        raise KeyError(conversation_id)
    if conv["status"] in ("running", "waiting_approval"):
        raise RuntimeError(f"conversation is {conv['status']}; cancel or answer it first")
    last = events.max_id(app, conversation_id)
    events.update(app, conversation_id, summary="", summary_cutoff=last)
    ev = events.append(
        app, conversation_id, "compaction", f"context cleared by {actor}",
        {"kind": "clear", "folded_through": last, "kept_from": last + 1, "summary_chars": 0},
    )
    app.bus.emit("event.appended", events.get(app, ev), conversation_id)
    return events.announce(app, conversation_id)  # type: ignore[return-value]
