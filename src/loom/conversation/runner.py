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

from .. import cancel, db, items, prompts
from ..ids import ulid
from ..models import roles
from ..models.types import PRIORITY_BACKGROUND, PRIORITY_INTERACTIVE, ChatRequest, Delta, Result, ToolCall
from ..tools import approvals, dispatch
from ..tools.registry import ToolContext, ToolResult
from . import compaction, events, inbox, status
from .context import CONTEXT_CLOSE, CONTEXT_OPEN, Context, assemble, image_urls, resolve_knowledge, strip_private, tool_image_followup
from .compaction import CompactionError

log = logging.getLogger(__name__)

INTERRUPTED = "interrupted — result lost (the backend restarted while this call was running); rerun it if still needed"

# A model call that failed after the wall clock ran far ahead of the monotonic clock was
# spanned by a machine suspend (Modern Standby), not by a bad provider: retry it for free
# (TD audit F7). On a platform whose `time.monotonic()` counts suspended time too the
# drift stays ~0, the branch never fires, and the behaviour is exactly today's.


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
    request_from_user: bool = False  # request_text is the user's own typed words on the desk
    last_assistant_text: str = ""  # this turn's newest non-empty step text; the intent fallback
    last_result: Result | None = None
    last_request: ChatRequest | None = None
    finished: dict[str, Any] | None = None
    cancelled: bool = False
    failed: str | None = None
    status_written: bool = False
    paused: bool = False  # ended waiting for an approval
    # The smallest ULID this turn can mint: processes and sub-threads with an id at or
    # above it were started by this turn, which is what a cancel stops (03 §6.4).
    floor_id: str = field(default_factory=lambda: ulid()[:10] + "0" * 16)

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
    # restart, so a persistent failure cannot spin (2026-09-21). A pause whose approvals
    # were all answered while the step still ran lost that answer's wake (the turn held
    # the lock), so it resumes itself (2026-10-13).
    if state is not None and state.failed is None and (
        inbox.pending_count(app, conversation_id) if not state.paused
        else not approvals.pending(app, conversation_id)
    ):
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
    state.request_text, state.request_from_user = _request(app, conv, claimed)
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
                ctx = await cancel.race(
                    app.cancel_flags, cid,
                    compaction.maybe_compact(app, conv, rebuild, priority=state.priority, steps_remaining=steps_line), "compaction",
                ) or ctx
            except cancel.Cancelled:
                state.cancelled = True
                break
            except CompactionError as exc:
                _system(app, cid, f"context near the limit and compaction could not reduce it: {exc}")

        result = await _call_model(app, state, ctx, rebuild, tools_enabled, steps_line, max_forced)
        if result is None:
            break  # failed; state.failed set, system event written
        state.steps += 1
        state.last_result = result
        if result.content:
            state.last_assistant_text = result.content

        if result.reasoning:
            ev = events.append(app, cid, "thinking", result.reasoning, {"step": state.steps})
            app.bus.emit("event.appended", events.get(app, ev), cid)

        # Usage: the provider's own token counts for this step, when it reported them
        # (either may be missing). Never estimated here — that is calibration's job
        # (§6.6); this is the raw number for the /usage endpoint and the UI captions.
        usage_meta: dict[str, int] = {}
        if result.usage.prompt_tokens is not None:
            usage_meta["prompt_tokens"] = result.usage.prompt_tokens
        if result.usage.completion_tokens is not None:
            usage_meta["completion_tokens"] = result.usage.completion_tokens

        if not result.tool_calls:
            meta = {"step": state.steps, "cancelled": state.cancelled, "finish_reason": result.finish_reason}
            if usage_meta:
                meta["usage"] = usage_meta
            ev = events.append(app, cid, "assistant", result.content, meta)
            row = events.get(app, ev)
            app.bus.emit("message.end", {"event_id": ev, "event": row}, cid)
            app.bus.emit("event.appended", row, cid)
            ctx.turn.append({"role": "assistant", "content": result.content})
            break

        # tool calls: persist first (persist before act), then dispatch
        calls_json = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in result.tool_calls]
        tool_call_meta = {"text": result.content, "step": state.steps, "reasoning_chars": len(result.reasoning or ""), "finish_reason": result.finish_reason}
        if usage_meta:
            tool_call_meta["usage"] = usage_meta
        ev = events.append(app, cid, "tool_call", db.dumps(calls_json), tool_call_meta)
        row = events.get(app, ev)
        app.bus.emit("message.end", {"event_id": ev, "event": row}, cid)
        app.bus.emit("event.appended", row, cid)
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": result.content or "",
            "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": db.dumps(c.arguments)}} for c in result.tool_calls],
        }
        if result.reasoning and app.settings.get("conversation.reasoning_replay"):
            # The end of the reasoning is what led to this step's calls; the start is the
            # plan, and when a step degenerated into repetition the start is also where
            # the loop's seed sits (a 68k-char loop was primed that way, 2026-09-21).
            cap = int(app.settings.get("conversation.reasoning_replay_max_chars"))
            text = result.reasoning
            if len(text) > cap:
                text = prompts.load("context/reasoning-elided") + "\n" + text[-cap:]
            assistant_msg["reasoning"] = text
        ctx.turn.append(assistant_msg)

        paused = await _dispatch_calls(app, state, ctx, ev, result.tool_calls, result.content or "", result.finish_reason)
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
    if state.cancelled:
        await _stop_turn_work(app, state)
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
    suspends = 0
    runaways = 0
    while True:
        messages = strip_private(ctx.messages(app, steps_line))
        if runaways:
            # One-call nudge after a runaway: appended here, never to ctx.turn, so the
            # retry sees it once and the transcript keeps only the system event.
            messages.append({"role": "user", "content": f"{CONTEXT_OPEN}\n{prompts.load('context/thinking-runaway')}\n{CONTEXT_CLOSE}"})
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
        mono0, wall0 = time.monotonic(), time.time()
        result = await _stream(app, state, request)
        slept = (time.time() - wall0) - (time.monotonic() - mono0)
        if result.ok or state.cancelled:
            return result
        # Suspend is a named event, not a provider fault: the wall clock ran ahead of the
        # monotonic one while the machine was in standby, so the stream-idle watchdog fired
        # on wake (TD audit F7). Retry for free — before the transient/overflow/fatal policy,
        # so the attempt is not charged to `model_error_retries` — and bound it so a clock
        # that keeps jumping cannot spin.
        if slept >= float(app.settings.get("conversation.suspend_drift_s")) and suspends < int(app.settings.get("conversation.suspend_retries")):
            suspends += 1
            minutes = max(1, int(slept // 60))
            log.info("model call on %s spanned a %s min suspend (%s); retrying", state.cid, minutes, result.error)
            _system(app, state.cid, f"system resumed after {minutes} min; retrying")
            continue
        if result.outcome == "runaway":
            # Reasoning that never reached a reply: retried with a nudge, never compacted
            # (read as overflow it folded away the plan the retry then re-derived,
            # 2026-10-21). The partial reasoning is kept for the transcript, not replayed.
            if result.reasoning:
                ev = events.append(app, state.cid, "thinking", result.reasoning, {"step": state.steps + 1, "capped": True})
                app.bus.emit("event.appended", events.get(app, ev), state.cid)
            runaways += 1
            if attempts < retries:
                attempts += 1
                _system(app, state.cid, f"{result.error}; retrying with a nudge to act")
                continue
            state.failed = f"reasoning ran past the thinking cap {runaways} time{'s' if runaways != 1 else ''}: {result.error}"
            _system(app, state.cid, state.failed)
            return None
        if result.outcome == "transient" and attempts < retries:
            attempts += 1
            log.info("transient model error on %s, retrying (%s)", state.cid, result.error)
            continue
        if result.outcome == "overflow" and state.forced_compactions < max_forced:
            state.forced_compactions += 1
            try:
                ctx = await cancel.race(
                    app.cancel_flags, state.cid,
                    compaction.maybe_compact(app, conv, rebuild, priority=state.priority, force=True), "compaction",
                ) or rebuild()
            except cancel.Cancelled:
                state.cancelled = True
                return Result(outcome="ok", finish_reason="cancelled")
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
    # The preset's Loom-side thinking cap (tokens, estimated as chars/4 × token_factor
    # like compaction.estimate). Providers may ignore the `reasoning` budget they are
    # sent — MiMo on OpenRouter honours only on/off (2026-10-22) — so this is enforced
    # here, on the stream, and never sent.
    cap = int(request.params.get("thinking_cap") or 0)
    factor = float(state.conv.get("token_factor") or 1.0)
    capped = False

    async def consume() -> None:
        nonlocal final, capped
        chars = 0
        async for ev in stream:
            if isinstance(ev, Delta):
                (content if ev.channel == "content" else reasoning).append(ev.text)
                app.bus.emit("message.delta", {"channel": ev.channel, "text": ev.text}, cid)
                if ev.channel == "reasoning" and cap > 0:
                    chars += len(ev.text)
                    if chars / 4 * factor > cap:
                        capped = True
                        return  # the finally below closes the HTTP stream
            else:
                final = ev

    try:
        # The whole call — queue wait, prompt processing, streaming — races the cancel
        # signal; cancelling it drops the queue waiter or closes the HTTP stream (03 §6.4).
        await cancel.race(app.cancel_flags, cid, consume(), "model call")
    except cancel.Cancelled:
        state.cancelled = True
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass
    if state.cancelled:
        return Result(outcome="ok", content="".join(content), reasoning="".join(reasoning), finish_reason="cancelled")
    if capped:
        return Result(
            outcome="runaway", content="".join(content), reasoning="".join(reasoning), finish_reason="thinking_cap",
            error=f"reasoning passed the thinking cap ({cap} tokens)",
        )
    if final is None:
        return Result(outcome="transient", error="stream ended without a result")
    if not final.content:
        final.content = "".join(content)
    if not final.reasoning:
        final.reasoning = "".join(reasoning)
    return final


# -- dispatch ----------------------------------------------------------------------------

def _tool_ctx(app, state: TurnState, call_id: str, event_id: int | None, intent: str = "", intent_carried: bool = False) -> ToolContext:  # type: ignore[no-untyped-def]
    return ToolContext(
        app=app, conversation=state.conv, call_id=call_id, event_id=event_id,
        request_text=state.request_text, request_from_user=state.request_from_user, intent=intent,
        intent_carried=intent_carried, priority=state.priority, meta={}, extras=state.extras,
    )


def _intent(app, state: TurnState, text: str) -> tuple[str, bool]:  # type: ignore[no-untyped-def]
    """`(intent, carried)`: what the turn said it was doing when it made this step's
    calls. The classifier reads it beside `request_text`, which for the desk is only the
    last user message ("go ahead") — too thin to judge a command by (TD audit F1).

    The fallback is this turn's own earlier text and nothing older: a bare call at the
    start of a turn has no intent, and "(none stated)" is honest where a reply from a
    previous day is not (2026-09-25). A carried intent is labelled as such for the
    judge: "promoting now — this restarts me" from step 3 was read as the intent of five
    later read-only calls and made each of them look suspicious (2026-09-28)."""
    if text:
        return text, False
    return state.last_assistant_text, bool(state.last_assistant_text)


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


async def _dispatch_calls(app, state: TurnState, ctx: Context, event_id: int, calls: list[ToolCall], text: str = "", finish_reason: str = "") -> bool:  # type: ignore[no-untyped-def]
    """Run the calls of one step. Returns True if the turn must pause for approval.
    A reply cut off at the output-token limit (`finish_reason == "length"`) is truncated
    or degenerate: none of its calls run. Within a reply, a call byte-identical to an
    earlier one does not run again. Both get a failed result per call id (2026-10-14).
    A reply with more calls than `conversation.max_calls_per_step` runs none of them
    either (a 473-call fs_list burst, 2026-10-21)."""
    cid = state.cid
    for c in calls:
        app.bus.emit("tool.call", {"call_id": c.id, "name": c.name, "args": c.arguments, "event_id": event_id}, cid)
    intent, carried = _intent(app, state, text)
    ctxs = {c.id: _tool_ctx(app, state, c.id, event_id, intent=intent, intent_carried=carried) for c in calls}
    outcomes: dict[str, Any] = {}
    max_calls = int(app.settings.get("conversation.max_calls_per_step"))
    if finish_reason == "length":
        for c in calls:
            outcomes[c.id] = ToolResult.error(prompts.load("context/tool-reply-truncated"))
    elif len(calls) > max_calls:
        refusal = prompts.load("context/tool-too-many-calls").replace("{count}", str(len(calls))).replace("{cap}", str(max_calls))
        for c in calls:
            outcomes[c.id] = ToolResult.error(refusal)
    else:
        first: dict[str, str] = {}
        for c in calls:
            key = c.name + "|" + json.dumps(c.arguments, sort_keys=True, separators=(",", ":"), default=str)
            if key in first:
                outcomes[c.id] = ToolResult.error(prompts.load("context/tool-duplicate-call").replace("{first_id}", first[key]))
            else:
                first[key] = c.id
    ro = [c for c in calls if c.id not in outcomes and _is_read_only(app, c)]
    if ro:
        results = await asyncio.gather(*(_dispatch_one(app, ctxs[c.id], c) for c in ro), return_exceptions=True)
        for c, r in zip(ro, results):
            outcomes[c.id] = ToolResult.error(f"dispatch crashed: {r!r}") if isinstance(r, BaseException) else r
    paused = False
    for c in calls:
        if c.id in outcomes:
            continue
        if paused:
            break  # not started; resumed by repair after the approval
        try:
            outcomes[c.id] = await _dispatch_one(app, ctxs[c.id], c)
        except Exception as exc:
            outcomes[c.id] = ToolResult.error(f"dispatch crashed: {exc!r}")
        if isinstance(outcomes[c.id], dispatch.ApprovalNeeded):
            paused = True
    any_pause = False
    followers: list[dict[str, Any]] = []
    for c in calls:
        out = outcomes.get(c.id)
        if out is None:
            continue
        if isinstance(out, dispatch.ApprovalNeeded):
            any_pause = True
            continue
        follower = _persist_result(app, state, ctx, c, out, ctxs[c.id])
        if follower is not None:
            followers.append(follower)
    # Image followers ride after the whole step's tool messages, in call order — the
    # same shape `messages_from_events` rebuilds on replay.
    ctx.turn.extend(followers)
    _absorb_extras(state, ctx)
    if any_pause:
        # dispatch already created the approval rows and set waiting_approval
        return True
    return False


async def _dispatch_one(app, tctx: ToolContext, call: ToolCall, approved: bool = False) -> ToolResult | dispatch.ApprovalNeeded:  # type: ignore[no-untyped-def]
    """One call through the dispatcher, under the turn's cancel signal (03 §6.4). The
    race is around the whole dispatch, so a cancel interrupts the judge waiting in the
    model queue or a hook as well as the handler (the cancellation reaches it through
    the dispatcher's own timeout wait; `shell` kills its tree). A call reached after
    the cancel never starts. Either way the call gets the one cancelled result.
    `approved`: the user allowed it already; steps 5–7 only (`dispatch.execute`)."""
    if approved:
        tool = app.tools.get(call.name)
        if tool is None:
            return ToolResult.error(f"unknown tool {call.name}")
        work = dispatch.execute(app, tctx, tool, call.arguments)
    else:
        work = dispatch.dispatch(app, tctx, call)
    try:
        return await cancel.race(app.cancel_flags, tctx.conversation_id, work, call.name)
    except cancel.Cancelled as exc:
        return dispatch.cancelled_result(exc.value)


def _persist_result(app, state: TurnState, ctx: Context | None, call: ToolCall, result: ToolResult, tctx: ToolContext) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    """Persist one result and append its tool message to the turn. Returns the image
    follower message (not yet appended) when the result carried images, else None."""
    meta = {"call_id": call.id, "name": call.name, "ok": result.ok, **{k: v for k, v in tctx.meta.items()}}
    if result.meta.get("cancelled"):
        meta["cancelled"] = True
    if result.meta.get("artifact_id"):
        meta["artifact_id"] = result.meta["artifact_id"]
    images = result.meta.get("images") or []
    if images:
        meta["images"] = images
    ev = events.append(app, state.cid, "tool_result", result.content, meta)
    row = events.get(app, ev)
    app.bus.emit(
        "tool.result",
        {"call_id": call.id, "name": call.name, "ok": result.ok, "preview": result.content[:400],
         "artifact_id": meta.get("artifact_id"), "event_id": ev, "policy": meta.get("policy"),
         "decision": meta.get("decision"), "reason": meta.get("reason"), "images": images,
         "cancelled": bool(meta.get("cancelled"))},
        state.cid,
    )
    app.bus.emit("event.appended", row, state.cid)
    if ctx is None:
        return None
    ctx.turn.append({"role": "tool", "tool_call_id": call.id, "content": result.content})
    return tool_image_followup(app, state.cid, call.id, images) if images else None


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
    state.request_text, state.request_from_user = _request(app, state.conv, [])
    state.priority = PRIORITY_INTERACTIVE if state.conv["kind"] == "desk" else PRIORITY_BACKGROUND
    # A call executed after an approval carries the intent it was made with, and the
    # resumed turn's later bare steps fall back to it like any other step text.
    resumed_intent = _resumed_intent(app, cid, tc)
    resumed_carried = bool(resumed_intent) and not (tc["meta"].get("text") or "").strip()
    state.last_assistant_text = resumed_intent
    # …and the step it was made at. A resumed turn continues its budget rather than
    # starting a fresh one (65 of 100 steps became "plenty of steps", TD audit F6). The
    # INTERRUPTED branch below deliberately leaves `steps` at 0: a turn whose results were
    # lost to a restart is not being resumed, it starts over.
    resumed_step = int(tc["meta"].get("step") or 0)
    did = False
    for c in missing:
        appr = approvals.pending_for_call(app, cid, c.id)  # latest row for the call, any status
        tctx = _tool_ctx(app, state, c.id, tc["id"], intent=resumed_intent, intent_carried=resumed_carried)
        if appr is not None and appr["status"] == "pending":
            return "waiting"
        if appr is not None:
            state.steps = max(state.steps, resumed_step)
        if appr is not None and appr["status"] == "allowed":
            result = await _dispatch_one(app, tctx, c, approved=True)  # type: ignore[assignment]
            tctx.meta.update({"policy": "ask", "decision": "allow", "reason": appr.get("reason") or "approved by user"})
        elif appr is not None and appr["status"] == "denied":
            result = dispatch.denied_result()
            tctx.meta.update({"policy": "ask", "decision": "deny", "reason": "denied by user"})
        elif paused:
            state.steps = max(state.steps, resumed_step)
            out = await _dispatch_one(app, tctx, c)
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


async def _stop_turn_work(app, state: TurnState) -> None:  # type: ignore[no-untyped-def]
    """A cancelled turn also stops what it started that outlives its own awaits (03
    §6.4): background processes and sub-threads it spawned that are still working. The
    awaits themselves were already interrupted by `cancel.race`, which noted each one.

    Persist before act: the one system event naming everything stopped — the turn's
    cancelled state in the transcript — is written first, then the processes are
    stopped and each sub-thread gets its own cancel (which, in turn, stops its own)."""
    cid = state.cid
    procs: list[dict[str, Any]] = []
    if app.processes is not None:
        procs = [dict(r) for r in app.db.execute(
            "SELECT id, title FROM processes WHERE conversation_id=? AND status IN ('starting', 'running') AND id >= ? ORDER BY id",
            (cid, state.floor_id),
        ).fetchall()]
    threads = [dict(r) for r in app.db.execute(
        "SELECT id, title FROM conversations WHERE parent_id=? AND kind='thread' AND id >= ? ORDER BY id",
        (cid, state.floor_id),
    ).fetchall() if _turn_alive(app, r["id"])]
    calls = app.cancel_flags.stopped(cid)
    parts = [f"{name} ×{calls.count(name)}" if calls.count(name) > 1 else name for name in dict.fromkeys(calls)]
    parts += [f'background process "{p["title"]}"' for p in procs]
    parts += [f'sub-thread "{t["title"]}"' for t in threads]
    text = "cancelled by user" + (f"; stopped: {', '.join(parts)}" if parts else "")
    meta = {"cancelled": True, "stopped": {"calls": calls, "processes": procs, "threads": threads}}
    ev = events.append(app, cid, "system", text, meta)
    app.bus.emit("event.appended", events.get(app, ev), cid)
    for t in threads:
        app.cancel_flags.add(t["id"])
    for p in procs:
        try:
            await app.processes.stop(p["id"], actor=f"conversation:{cid}")
        except Exception as exc:  # one process that will not die must not keep the others alive
            log.warning("cancel: could not stop process %s: %r", p["id"], exc)
            _system(app, cid, f'could not stop background process "{p["title"]}": {exc}')


def _turn_alive(app, conversation_id: str) -> bool:  # type: ignore[no-untyped-def]
    """A conversation whose turn is scheduled or running."""
    task = app.tasks.get(f"turn:{conversation_id}")
    lock = app.turn_locks.get(conversation_id)
    return (task is not None and not task.done()) or (lock is not None and lock.locked())


async def _status_safety_net(app, state: TurnState) -> None:  # type: ignore[no-untyped-def]
    """Threads: a turn that wrote no status gets one at turn end (03 §6.3 step 5)."""
    conv = state.conv
    try:
        fields, owner, _sink = status.read(app, conv)
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
        status.write(app, conv, new_fields, owner=owner or state.cid)
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
        events.set_status(app, state.cid, final, finished_at=now, walkthrough=state.finished.get("walkthrough_path"))
        _notify_parent(app, state, final, state.finished.get("summary") or "", delivery=state.finished.get("delivery"))
    elif state.failed:
        events.set_status(app, state.cid, "failed")
        _notify_parent(app, state, "failed", state.failed)
    elif state.cancelled:
        events.set_status(app, state.cid, "idle")
    else:
        events.set_status(app, state.cid, "waiting_input")
        text = (state.last_result.content if state.last_result else "") or "(no message)"
        _notify_parent(app, state, "waiting", text)


def _notify_parent(app, state: TurnState, event: str, text: str, delivery: str | None = None) -> None:  # type: ignore[no-untyped-def]
    """`text` is the short form (the finish summary): it is the now-board item, truncated.
    `delivery`, when the caller has one, is the summary plus the finish evidence block —
    the body the parent conversation actually reads, so the walkthrough reaches the desk
    instead of only being filed (TD audit §3)."""
    conv = state.conv
    parent = conv.get("parent_id")
    title = conv.get("title") or (conv.get("goal") or "")[:60] or conv["id"]
    # "waiting" is any turn that ended in plain text; the harness cannot tell a question
    # from a report, so the header says what happened, not what the thread wants
    # (three of four finished threads ended with a report instead of finish, 2026-09-22).
    label = {"done": "finished", "failed": "failed", "waiting": "stopped without finishing", "blocked": "blocked"}.get(event, event)
    body = text.strip()
    if parent:
        from . import post

        post(app, parent, f"[{label}]\n{(delivery or text).strip()}", source=f"conversation:{conv['id']}", meta={"title": title, "kind": "thread", "event": event})
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
        meta: dict[str, Any] = {"inbox_id": r["id"], "source": r["source"]}
        images = (r.get("meta") or {}).get("images") or []
        if images:
            meta["images"] = images
        ev = events.append(app, state.cid, "user", text, meta)
        row = events.get(app, ev)
        app.bus.emit("event.appended", row, state.cid)
        if ctx is not None:
            msg: dict[str, Any] = {"role": "user", "content": text}
            urls = image_urls(app, state.cid, images) if images else []
            if urls:
                msg["images"] = urls
            ctx.turn.append(msg)


def _request(app, conv: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[str, bool]:  # type: ignore[no-untyped-def]
    """(request_text, from_user) for the classifier: the claimed rows' text, else the
    last user event (a repaired turn claims nothing). `from_user` is true when every row
    is the user's own typed message (source "user"); a thread's goal or handoff, a
    thread report, a connector or schedule message is someone else's text and stays
    untrusted data (03 §9.5, 2026-09-25), which the judge is not shown at all
    (2026-10-12). A thread turn that carries none of the user's words falls back to the
    user's request the thread was started from (`conversations.user_request`,
    2026-10-19)."""
    if rows:
        text = "\n".join(r.get("content") or "" for r in rows)
        from_user = all(inbox.is_user_source(r) for r in rows)
    else:
        last = events.last(app, conv["id"], 1, kinds=["user"])
        text = (last[0]["content"] or "") if last else ""
        from_user = bool(last) and (last[0]["meta"].get("source") or "user") == "user"
    if from_user and text.strip():
        return text, True
    started_from = (conv.get("user_request") or "").strip()
    if started_from:
        return started_from, True
    return text, False


def _resumed_intent(app, cid: str, tc: dict[str, Any]) -> str:  # type: ignore[no-untyped-def]
    """The intent of a call resumed by `_repair`: the preamble the model wrote in the
    step that issued it (persisted on the tool_call event), else the newest text the
    same turn said before it — `assistant` content or a `tool_call` preamble after the
    turn's last user message. Never an earlier turn's."""
    own = tc["meta"].get("text") or ""
    if own.strip():
        return own
    user = events.list_events(app, cid, before=tc["id"], limit=1, kinds=["user"], descending=True)
    since = user[0]["id"] if user else 0
    for ev in reversed(events.list_events(app, cid, after=since, before=tc["id"], limit=10_000, kinds=["assistant", "tool_call"])):
        text = (ev["content"] if ev["kind"] == "assistant" else ev["meta"].get("text")) or ""
        if text.strip():
            return text
    return ""


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
