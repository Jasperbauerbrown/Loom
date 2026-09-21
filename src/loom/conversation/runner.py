"""`run_turn` — the only agent loop (03 §6.3). One codepath for desk and threads, for
user messages and trigger events, for first runs and resumes after approval or crash.

Turn outline:
  0 lock (per conversation; held → return: the running turn claims the inbox itself)
  1 repair unpaired tool calls (pending approval → wait; decided → execute/deny;
    paused deliberately → dispatch; crashed → synthetic "interrupted" result)
  2 claim inbox → nothing and no repair → return
  3 status running; persist claimed rows as user events; assemble context
  4 step loop: brakes → model call (retry/overflow/fatal policy) → persist thinking +
    assistant|tool_call → dispatch (read-only concurrently, others sequentially; an
    `ask` pauses the turn) → persist results → mid-turn inbox → budget
  5 post-turn: calibrate, compact, thread status safety net, deliver inbox, final
    status, thread event to parent
  6 unlock
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .. import db, items, prompts
from ..models import roles
from ..models.types import PRIORITY_BACKGROUND, PRIORITY_INTERACTIVE, ChatRequest, Delta, Result, ToolCall
from ..tools import approvals, dispatch
from ..tools.registry import ToolContext, ToolResult
from . import compaction, events, inbox
from .context import Context, assemble, resolve_knowledge, strip_private
from .compaction import CompactionError

log = logging.getLogger(__name__)

INTERRUPTED = "interrupted — result lost (the backend restarted while this call was running); rerun it if still needed"


@dataclass
class TurnState:
    conv: dict[str, Any]
    priority: int
    claimed_ids: list[int] = field(default_factory=list)
    last_claimed_id: int = 0
    turn_start_event_id: int | None = None
    steps: int = 0
    forced_compactions: int = 0
    extras: dict[str, Any] = field(default_factory=dict)  # shared with every ToolContext this turn
    request_text: str = ""
    last_result: Result | None = None
    last_request: ChatRequest | None = None
    finished: dict[str, Any] | None = None
    cancelled: bool = False
    failed: str | None = None
    status_written: bool = False
    paused: bool = False  # ended waiting for an approval

    @property
    def cid(self) -> str:
        return self.conv["id"]


# -- public entry points ---------------------------------------------------------------

def wake(app, conversation_id: str) -> None:  # type: ignore[no-untyped-def]
    """Schedule a turn unless one is running (a running turn claims the inbox itself)."""
    task = app.tasks.get(f"turn:{conversation_id}")
    if task is not None and not task.done():
        return
    if app.lock_for(conversation_id).locked():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Only reachable from a thread without the loop (a sync endpoint run in a
        # threadpool). Every endpoint is `async def` so this is a bug, not a state.
        log.warning("wake(%s) called outside the event loop; the next start() resumes it", conversation_id)
        return
    app.spawn(f"turn:{conversation_id}", run_turn(app, conversation_id))


async def run_turn(app, conversation_id: str) -> None:  # type: ignore[no-untyped-def]
    lock = app.lock_for(conversation_id)
    if lock.locked():
        return
    state: TurnState | None = None
    async with lock:
        conv = app.conversation(conversation_id)
        if conv is None or conv["status"] == "archived":
            return
        state = TurnState(conv=conv, priority=PRIORITY_BACKGROUND)
        try:
            await _turn(app, state)
        except Exception as exc:  # last line of defence: never leave a conversation stuck in `running`
            log.exception("turn crashed for %s", conversation_id)
            _system(app, state.cid, f"turn crashed: {exc!r}")
            state.failed = repr(exc)
            _finish_status(app, state)
        finally:
            app.cancel_flags.discard(conversation_id)
    # Rows posted during this turn's last model call had no step boundary left to be
    # claimed at (the mid-turn claim runs after tool dispatch only). A clean turn re-wakes
    # for them; a paused or failed one leaves them for the approval / next message /
    # restart, so a persistent failure cannot spin (2026-09-21).
    if state is not None and not state.paused and state.failed is None and inbox.pending_count(app, conversation_id):
        asyncio.get_running_loop().call_soon(wake, app, conversation_id)


# -- the turn ----------------------------------------------------------------------------

async def _turn(app, state: TurnState) -> None:  # type: ignore[no-untyped-def]
    conv = state.conv
    cid = state.cid
    paused_before = conv["status"] == "waiting_approval"

    # 1 repair
    repaired = await _repair(app, state, paused_before)
    if repaired == "waiting":
        state.paused = True
        return

    # 2 claim
    claimed = inbox.claim(app, cid)
    if not claimed and not repaired:
        return
    state.priority = PRIORITY_INTERACTIVE if conv["kind"] == "desk" or any(inbox.is_user_source(r) for r in claimed) else PRIORITY_BACKGROUND

    # 3 running; persist user events; assemble
    events.set_status(app, cid, "running")
    conv["status"] = "running"
    if not repaired:
        # rows already persisted by an unfinished turn keep their events in this turn's
        # `turn` slice so the knowledge block is spliced before them, not after
        persisted = [int(r["persisted_event_id"]) for r in claimed if r.get("persisted_event_id")]
        state.turn_start_event_id = min([events.max_id(app, cid) + 1, *persisted])
    _persist_claimed(app, state, claimed)
    state.request_text = "\n".join(r.get("content") or "" for r in claimed) or _last_user_text(app, cid)
    resolved = await resolve_knowledge(app, conv, "\n".join(r.get("content") or "" for r in claimed))
    ctx = assemble(app, conv, claimed, state.turn_start_event_id, resolved=resolved)

    def rebuild() -> Context:
        fresh = app.conversation(cid)
        conv.update(fresh or {})
        c = assemble(app, conv, [], state.turn_start_event_id, resolved=resolved)
        c.exposed = ctx.exposed if ctx else set()
        return c

    budget = int(conv.get("step_budget") or app.settings.get(
        "conversation.step_budget_desk" if conv["kind"] == "desk" else "conversation.step_budget_thread"
    ))
    max_forced = int(app.settings.get("conversation.forced_compactions_per_turn"))

    # 4 loop
    while True:
        if cid in app.cancel_flags:
            state.cancelled = True
            _system(app, cid, "cancelled by user")
            break
        remaining = budget - state.steps
        if remaining <= 0:
            break
        tools_enabled = remaining > 1
        steps_line = remaining if conv["kind"] == "thread" else None

        # brakes: in-flight estimate ≥ limit → compact now (bounded per turn). The estimate
        # is approximate; when nothing can be compacted the call proceeds and the model's
        # own `overflow` outcome is the authoritative failure.
        _e, _m, limit = compaction.limits(app, conv)
        if compaction.estimate(app, conv, ctx, steps_line) >= limit and state.forced_compactions < max_forced:
            state.forced_compactions += 1
            try:
                ctx = await compaction.maybe_compact(app, conv, rebuild, priority=state.priority, steps_remaining=steps_line) or ctx
            except CompactionError as exc:
                _system(app, cid, f"context near the limit and compaction could not reduce it: {exc}")

        result = await _call_model(app, state, ctx, rebuild, tools_enabled, steps_line, max_forced)
        if result is None:
            break  # failed; state.failed set, system event written
        state.steps += 1
        state.last_result = result

        if result.reasoning:
            ev = events.append(app, cid, "thinking", result.reasoning, {"step": state.steps})
            app.bus.emit("event.appended", events.get(app, ev), cid)

        if not result.tool_calls:
            ev = events.append(app, cid, "assistant", result.content, {"step": state.steps, "cancelled": state.cancelled, "finish_reason": result.finish_reason})
            row = events.get(app, ev)
            app.bus.emit("message.end", {"event_id": ev, "event": row}, cid)
            app.bus.emit("event.appended", row, cid)
            ctx.turn.append({"role": "assistant", "content": result.content})
            break

        # tool calls: persist first (persist before act), then dispatch
        calls_json = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in result.tool_calls]
        ev = events.append(app, cid, "tool_call", db.dumps(calls_json), {"text": result.content, "step": state.steps, "reasoning_chars": len(result.reasoning or "")})
        row = events.get(app, ev)
        app.bus.emit("message.end", {"event_id": ev, "event": row}, cid)
        app.bus.emit("event.appended", row, cid)
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": result.content or "",
            "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": db.dumps(c.arguments)}} for c in result.tool_calls],
        }
        if result.reasoning and app.settings.get("conversation.reasoning_replay"):
            cap = int(app.settings.get("conversation.reasoning_replay_max_chars"))
            assistant_msg["reasoning"] = result.reasoning[:cap]
        ctx.turn.append(assistant_msg)

        paused = await _dispatch_calls(app, state, ctx, ev, result.tool_calls)
        if paused:
            state.paused = True
            return  # status is waiting_approval; resume re-enters at step 1
        if state.finished is not None:
            break

        # mid-turn inbox
        new_rows = inbox.claim(app, cid, after_id=state.last_claimed_id)
        if new_rows:
            _persist_claimed(app, state, new_rows, ctx)

    # 5 post-turn
    _post_turn(app, state)
    if conv["kind"] == "thread" and not state.status_written and not state.cancelled and state.steps > 0:
        await _status_safety_net(app, state)
    try:
        await compaction.maybe_compact(app, conv, lambda: assemble(app, conv, [], None), priority=max(state.priority, PRIORITY_BACKGROUND))
    except CompactionError as exc:
        _system(app, cid, f"compaction failed between turns: {exc}")
    except Exception:
        log.exception("post-turn compaction failed for %s", cid)
    inbox.mark_delivered(app, state.claimed_ids)
    _finish_status(app, state)


# -- model call with outcome policy (03 §5.5) ------------------------------------------

async def _call_model(app, state: TurnState, ctx: Context, rebuild, tools_enabled: bool, steps_line: int | None, max_forced: int) -> Result | None:  # type: ignore[no-untyped-def]
    conv = state.conv
    retries = int(app.settings.get("conversation.model_error_retries"))
    attempts = 0
    role = conv["role"]
    preset = conv.get("preset")
    tried_fallback = False
    while True:
        messages = strip_private(ctx.messages(app, steps_line))
        try:
            request = roles.build_request(
                app, role=role, preset=preset, messages=messages, tools=ctx.tool_schemas(app, tools_enabled),
                conversation_id=state.cid, purpose="turn",
            )
        except Exception as exc:  # no model configured for the role
            state.failed = f"model unavailable: {exc}"
            _system(app, state.cid, state.failed)
            return None
        state.last_request = request
        result = await _stream(app, state, request)
        if result.ok or state.cancelled:
            return result
        if result.outcome == "transient" and attempts < retries:
            attempts += 1
            log.info("transient model error on %s, retrying (%s)", state.cid, result.error)
            continue
        if result.outcome == "overflow" and state.forced_compactions < max_forced:
            state.forced_compactions += 1
            try:
                ctx = await compaction.maybe_compact(app, conv, rebuild, priority=state.priority, force=True) or rebuild()
            except CompactionError as exc:
                state.failed = f"context overflow and compaction failed: {exc}"
                _system(app, state.cid, state.failed)
                return None
            continue
        if result.outcome == "fatal" and not tried_fallback:
            fb = roles.fallback(app)
            if fb and fb != role:
                tried_fallback = True
                role, preset = fb, None
                _system(app, state.cid, f"model call failed ({result.error}); trying fallback role '{fb}'")
                continue
        state.failed = f"model call failed ({result.outcome}): {result.error}"
        _system(app, state.cid, state.failed)
        return None


async def _stream(app, state: TurnState, request: ChatRequest) -> Result:  # type: ignore[no-untyped-def]
    cid = state.cid
    app.bus.emit("message.start", {"role": "assistant", "step": state.steps + 1}, cid)
    content: list[str] = []
    reasoning: list[str] = []
    final: Result | None = None
    stream = app.models.stream(request, state.priority)
    try:
        async for ev in stream:
            if isinstance(ev, Delta):
                (content if ev.channel == "content" else reasoning).append(ev.text)
                app.bus.emit("message.delta", {"channel": ev.channel, "text": ev.text}, cid)
                if cid in app.cancel_flags:
                    state.cancelled = True
                    break
            else:
                final = ev
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass
    if state.cancelled:
        return Result(outcome="ok", content="".join(content), reasoning="".join(reasoning), finish_reason="cancelled")
    if final is None:
        return Result(outcome="transient", error="stream ended without a result")
    if not final.content:
        final.content = "".join(content)
    if not final.reasoning:
        final.reasoning = "".join(reasoning)
    return final


# -- dispatch ----------------------------------------------------------------------------

def _tool_ctx(app, state: TurnState, call_id: str, event_id: int | None) -> ToolContext:  # type: ignore[no-untyped-def]
    return ToolContext(
        app=app, conversation=state.conv, call_id=call_id, event_id=event_id,
        request_text=state.request_text, priority=state.priority, meta={}, extras=state.extras,
    )


def _is_read_only(app, call: ToolCall) -> bool:  # type: ignore[no-untyped-def]
    tool = app.tools.get(call.name)
    if tool is None:
        return True  # unknown tools produce an error result; running them "concurrently" is harmless
    if tool.read_only:
        return True
    fn = getattr(tool, "read_only_for", None)
    try:
        return bool(fn and fn(call.arguments))
    except Exception:
        return False


async def _dispatch_calls(app, state: TurnState, ctx: Context, event_id: int, calls: list[ToolCall]) -> bool:  # type: ignore[no-untyped-def]
    """Run the calls of one step. Returns True if the turn must pause for approval."""
    cid = state.cid
    for c in calls:
        app.bus.emit("tool.call", {"call_id": c.id, "name": c.name, "args": c.arguments, "event_id": event_id}, cid)
    ctxs = {c.id: _tool_ctx(app, state, c.id, event_id) for c in calls}
    outcomes: dict[str, Any] = {}
    ro = [c for c in calls if _is_read_only(app, c)]
    if ro:
        results = await asyncio.gather(*(dispatch.dispatch(app, ctxs[c.id], c) for c in ro), return_exceptions=True)
        for c, r in zip(ro, results):
            outcomes[c.id] = ToolResult.error(f"dispatch crashed: {r!r}") if isinstance(r, BaseException) else r
    paused = False
    for c in calls:
        if c.id in outcomes:
            continue
        if paused:
            break  # not started; resumed by repair after the approval
        try:
            outcomes[c.id] = await dispatch.dispatch(app, ctxs[c.id], c)
        except Exception as exc:
            outcomes[c.id] = ToolResult.error(f"dispatch crashed: {exc!r}")
        if isinstance(outcomes[c.id], dispatch.ApprovalNeeded):
            paused = True
    any_pause = False
    for c in calls:
        out = outcomes.get(c.id)
        if out is None:
            continue
        if isinstance(out, dispatch.ApprovalNeeded):
            any_pause = True
            continue
        _persist_result(app, state, ctx, c, out, ctxs[c.id])
    _absorb_extras(state, ctx)
    if any_pause:
        # dispatch already created the approval rows and set waiting_approval
        return True
    return False


def _persist_result(app, state: TurnState, ctx: Context | None, call: ToolCall, result: ToolResult, tctx: ToolContext) -> None:  # type: ignore[no-untyped-def]
    meta = {"call_id": call.id, "name": call.name, "ok": result.ok, **{k: v for k, v in tctx.meta.items()}}
    if result.meta.get("artifact_id"):
        meta["artifact_id"] = result.meta["artifact_id"]
    ev = events.append(app, state.cid, "tool_result", result.content, meta)
    row = events.get(app, ev)
    app.bus.emit(
        "tool.result",
        {"call_id": call.id, "name": call.name, "ok": result.ok, "preview": result.content[:400],
         "artifact_id": meta.get("artifact_id"), "event_id": ev, "policy": meta.get("policy"),
         "decision": meta.get("decision"), "reason": meta.get("reason")},
        state.cid,
    )
    app.bus.emit("event.appended", row, state.cid)
    if ctx is not None:
        ctx.turn.append({"role": "tool", "tool_call_id": call.id, "content": result.content})


def _absorb_extras(state: TurnState, ctx: Context) -> None:
    if "exposed" in state.extras:
        ctx.exposed |= set(state.extras["exposed"])
    if state.extras.get("status_written"):
        state.status_written = True
    if state.extras.get("finish") is not None:
        state.finished = state.extras["finish"]


# -- repair (03 §6.3 step 1) -------------------------------------------------------------

async def _repair(app, state: TurnState, paused: bool) -> str | bool:  # type: ignore[no-untyped-def]
    """Returns "waiting" if an approval is still pending, True if results were
    synthesised/executed, False if nothing needed repair."""
    cid = state.cid
    last = events.last(app, cid, 1, kinds=["tool_call"])
    if not last:
        return False
    tc = last[0]
    later = events.list_events(app, cid, after=tc["id"], limit=10_000)
    if any(e["kind"] in ("assistant", "user") for e in later):
        return False  # the transcript moved on; nothing dangling
    present = {e["meta"].get("call_id") for e in later if e["kind"] == "tool_result"}
    calls = [ToolCall(id=c["id"], name=c["name"], arguments=c.get("arguments", {})) for c in db.loads(tc["content"], [])]
    missing = [c for c in calls if c.id not in present]
    if not missing:
        return False
    state.request_text = _last_user_text(app, cid)
    state.priority = PRIORITY_INTERACTIVE if state.conv["kind"] == "desk" else PRIORITY_BACKGROUND
    did = False
    for c in missing:
        appr = approvals.pending_for_call(app, cid, c.id)  # latest row for the call, any status
        tctx = _tool_ctx(app, state, c.id, tc["id"])
        if appr is not None and appr["status"] == "pending":
            return "waiting"
        if appr is not None and appr["status"] == "allowed":
            tool = app.tools.get(c.name)
            result = await dispatch.execute(app, tctx, tool, c.arguments) if tool else ToolResult.error(f"unknown tool {c.name}")
            tctx.meta.update({"policy": "ask", "decision": "allow", "reason": appr.get("reason") or "approved by user"})
        elif appr is not None and appr["status"] == "denied":
            result = dispatch.denied_result()
            tctx.meta.update({"policy": "ask", "decision": "deny", "reason": "denied by user"})
        elif paused:
            out = await dispatch.dispatch(app, tctx, c)
            if isinstance(out, dispatch.ApprovalNeeded):
                _absorb_extras(state, Context(state.conv, [], [], [], [], ""))
                return "waiting"
            result = out
        else:
            result = ToolResult.error(INTERRUPTED)
            tctx.meta.update({"policy": "n/a", "decision": "interrupted", "reason": "backend restarted mid-call"})
        _persist_result(app, state, None, c, result, tctx)
        did = True
    _absorb_extras(state, Context(state.conv, [], [], [], [], ""))
    if did:
        state.turn_start_event_id = tc["id"]
    return did


# -- post-turn ---------------------------------------------------------------------------

def _post_turn(app, state: TurnState) -> None:  # type: ignore[no-untyped-def]
    if state.failed or state.cancelled or state.last_result is None or state.last_request is None:
        return
    usage = state.last_result.usage
    compaction.calibrate(app, state.conv, usage.prompt_tokens, state.last_request.chars())


async def _status_safety_net(app, state: TurnState) -> None:  # type: ignore[no-untyped-def]
    """Threads: a turn that wrote no status gets one at turn end (03 §6.3 step 5)."""
    conv = state.conv
    path = conv.get("scope_note")
    if not path:
        return
    try:
        fields, owner = app.vault.read_status(path)
    except Exception:
        fields, owner = None, None
    turn_events = events.list_events(app, state.cid, after=(state.turn_start_event_id or 1) - 1, limit=10_000)
    rendered = compaction.render_for_summary([e for e in turn_events if e["kind"] != "thinking"])
    user_text = (
        f"# Goal\n{conv.get('goal') or ''}\n\n# Current status block\n{db.dumps(fields or {})}\n\n# This turn's events\n{rendered}"
    )
    role = "utility" if app.has_model("utility") else conv["role"]
    try:
        request = roles.build_request(
            app, role=role, preset=None,
            messages=[{"role": "system", "content": prompts.load("status-safety-net")}, {"role": "user", "content": user_text}],
            conversation_id=state.cid, purpose="status",
        )
        result = await app.models.chat(request, max(state.priority, PRIORITY_BACKGROUND))
        if not result.ok:
            raise RuntimeError(result.error)
        new_fields = _extract_json(result.content)
        if state.finished is not None:
            new_fields["state"] = "done" if state.finished.get("state", "done") == "done" else "blocked"
        app.vault.write_status(path, owner or state.cid, new_fields)
        app.bus.emit("knowledge.changed", {"paths": [path]}, state.cid)
    except Exception as exc:
        log.warning("status safety net failed for %s: %s", state.cid, exc)
        _system(app, state.cid, f"status safety net failed: {exc}")


def _finish_status(app, state: TurnState) -> None:  # type: ignore[no-untyped-def]
    conv = app.conversation(state.cid) or state.conv
    if conv["status"] == "waiting_approval" and state.paused:
        return
    kind = conv["kind"]
    now = db.now_iso()
    if kind == "desk":
        events.set_status(app, state.cid, "idle")
        return
    # thread
    if state.finished is not None:
        final = "done" if state.finished.get("state", "done") == "done" else "failed"
        events.set_status(app, state.cid, final, finished_at=now)
        _notify_parent(app, state, final, state.finished.get("summary") or "")
    elif state.failed:
        events.set_status(app, state.cid, "failed")
        _notify_parent(app, state, "failed", state.failed)
    elif state.cancelled:
        events.set_status(app, state.cid, "idle")
    else:
        events.set_status(app, state.cid, "waiting_input")
        text = (state.last_result.content if state.last_result else "") or "(no message)"
        _notify_parent(app, state, "waiting", text)


def _notify_parent(app, state: TurnState, event: str, text: str) -> None:  # type: ignore[no-untyped-def]
    conv = state.conv
    parent = conv.get("parent_id")
    title = conv.get("title") or (conv.get("goal") or "")[:60] or conv["id"]
    label = {"done": "finished", "failed": "failed", "waiting": "needs input", "blocked": "blocked"}.get(event, event)
    body = text.strip()
    if parent:
        from . import post

        post(app, parent, f"[{label}]\n{body}", source=f"conversation:{conv['id']}", meta={"title": title, "kind": "thread", "event": event})
    policy = app.settings.get("triggers.policy_thread_event")
    items.post(
        app, "thread_event", f'Thread "{title}" {label}', body[:2000], conversation_id=conv["id"],
        source=f"conversation:{conv['id']}", action={"kind": "open_conversation", "conversation_id": conv["id"]},
        notify=(policy == "interrupt"),
    )


# -- helpers -----------------------------------------------------------------------------

def _persist_claimed(app, state: TurnState, rows: list[dict[str, Any]], ctx: Context | None = None) -> None:  # type: ignore[no-untyped-def]
    for r in rows:
        state.claimed_ids.append(r["id"])
        state.last_claimed_id = max(state.last_claimed_id, r["id"])
        if r.get("persisted_event_id"):
            continue  # already in the transcript from an unfinished turn (approval pause / crash)
        text = inbox.render(app, r)
        ev = events.append(app, state.cid, "user", text, {"inbox_id": r["id"], "source": r["source"]})
        row = events.get(app, ev)
        app.bus.emit("event.appended", row, state.cid)
        if ctx is not None:
            ctx.turn.append({"role": "user", "content": text})


def _last_user_text(app, cid: str) -> str:  # type: ignore[no-untyped-def]
    last = events.last(app, cid, 1, kinds=["user"])
    return last[0]["content"] if last else ""


def _system(app, cid: str, text: str) -> None:  # type: ignore[no-untyped-def]
    ev = events.append(app, cid, "system", text)
    app.bus.emit("event.appended", events.get(app, ev), cid)


def _extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in response")
    obj = json.loads(text[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("JSON is not an object")
    return obj
