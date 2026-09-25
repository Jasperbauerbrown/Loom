"""Before/after-tool hooks declared by connectors (05-backlog, audit #14).

The dispatcher is the only caller. `before` runs once a call has been allowed (by
policy, by the pre-filter, by the judge, by a conversation grant or by desk
auto-approve) and before it executes: a hook may annotate the call or turn the allow
into an ask, never deny. `after` runs on every executed result, including a call the
runner executes after an approval, and may annotate the result. A hook that raises is
logged and ignored: hooks narrow or annotate, they cannot break the call.

This module is the contract; the connector-side declaration and the loader wiring are
the implementation (see 03 §10.2).

The connector side is two optional fields on `Connector` (`loom.connectors.base`)::

    CONNECTOR = Connector(..., before_tool=my_before, after_tool=my_after)

    async def before_tool(api, config, ctx_summary, tool_name, args) -> dict | None
    async def after_tool(api, config, ctx_summary, tool_name, args, result) -> dict | None

`before_tool` answers `{"ask": bool, "reason": str, "notes": [str]}` and `after_tool`
`{"notes": [str]}`; `None`, `{}` and unknown keys say nothing. `ctx_summary` is a plain
dict and `args` a copy, so a hook reads the call but can never change what executes.
Every hook is bounded by `HOOK_TIMEOUT_S`; one that raises, times out or answers the
wrong type is logged and skipped. `before` and `after` never raise.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any

from .. import prompts
from .registry import Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)

# One hook call may take this long. The same spirit as `dispatch.DEFAULT_TIMEOUT_S`: a
# connector that hangs annotating a call must not hang the turn.
HOOK_TIMEOUT_S = 10.0
# How much of the result's text an after-hook sees. A hook annotates; it does not need
# the whole of a bounded `fs_read` page.
HOOK_RESULT_CHARS = 4000


@dataclass
class HookVerdict:
    ask: bool = False  # turn this allow into an ask
    reason: str = ""  # why; becomes the approval's reason
    notes: list[str] = field(default_factory=list)  # annotations the model sees on the result


# ---------------------------------------------------------------------------------
# what a hook is given: plain data, never the live objects
# ---------------------------------------------------------------------------------
def _records(app: Any) -> list[Any]:
    """The loaded connectors that declare a hook. `app.connectors` is absent on the
    partial apps the CLI and some tests build, so this is best-effort throughout."""
    loader = getattr(app, "connectors", None)
    getter = getattr(loader, "hooks", None) if loader is not None else None
    if getter is None:
        return []
    try:
        return list(getter() or [])
    except Exception as exc:
        log.warning("hooks: listing connector hooks failed: %r", exc)
        return []


def _summary(ctx: ToolContext) -> dict[str, Any]:
    """The `ctx_summary` a hook sees: what the call is for, and how it came to be
    allowed. A plain dict, never the live `ToolContext`."""
    conversation = getattr(ctx, "conversation", None) or {}
    meta = getattr(ctx, "meta", None) or {}
    return {
        "conversation_id": str(conversation.get("id") or ""),
        "conversation_kind": str(conversation.get("kind") or ""),
        "conversation_goal": str(conversation.get("goal") or conversation.get("title") or ""),
        "call_id": str(getattr(ctx, "call_id", "") or ""),
        "intent": str(getattr(ctx, "intent", "") or ""),
        "request_text": str(getattr(ctx, "request_text", "") or ""),
        "policy": str(meta.get("policy") or ""),
        "decision": str(meta.get("decision") or ""),
        "reason": str(meta.get("reason") or ""),
    }


def _copy(value: Any) -> Any:
    """A hook must not be able to mutate what executes."""
    try:
        return copy.deepcopy(value)
    except Exception:
        return dict(value) if isinstance(value, dict) else value


def _result_summary(result: ToolResult) -> dict[str, Any]:
    content = result.content if isinstance(result.content, str) else str(result.content)
    return {
        "ok": bool(result.ok),
        "content": content[:HOOK_RESULT_CHARS],
        "meta": _copy(result.meta if isinstance(result.meta, dict) else {}),
    }


def _marker(connector: str, note: str) -> str:
    return prompts.load("context/tool-hook-note").replace("{connector}", connector).replace("{note}", note)


def _notes(connector: str, payload: dict[str, Any]) -> list[str]:
    value = payload.get("notes")
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [_marker(connector, str(note).strip()) for note in value if str(note).strip()]


async def _call(record: Any, which: str, fn: Any, arguments: tuple[Any, ...]) -> dict[str, Any] | None:
    """One hook call. Bounded, and every failure class ends the same way: logged, None.

    A sync hook is accepted (a pure annotation needs no await) but runs inline, so only
    the awaitable part is bounded: a hook that does I/O declares `async def`."""
    name = str(getattr(record, "name", "?"))
    try:
        value = fn(*arguments)
        if inspect.isawaitable(value):
            value = await asyncio.wait_for(value, timeout=HOOK_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except (asyncio.TimeoutError, TimeoutError):
        log.warning("hooks: %s %s timed out after %gs; ignored", name, which, HOOK_TIMEOUT_S)
        return None
    except Exception as exc:
        log.warning("hooks: %s %s raised (%r); ignored", name, which, exc)
        return None
    if value is None:
        return None
    if not isinstance(value, dict):
        log.warning("hooks: %s %s returned %s, not a dict; ignored", name, which, type(value).__name__)
        return None
    return value


# ---------------------------------------------------------------------------------
# the two entry points the dispatcher calls
# ---------------------------------------------------------------------------------
async def before(app: Any, ctx: ToolContext, tool: Tool, args: dict[str, Any]) -> HookVerdict:
    """Consult every enabled connector's before-hook.

    A hook may annotate the call or ask for approval. It may not deny: a returned
    `deny` is logged and counted as an ask, the strongest thing a connector can say
    (the hard rule — nothing static ever denies). `ask` is true if any hook asks, and
    `reason` names each connector that did."""
    verdict = HookVerdict()
    records = [r for r in _records(app) if getattr(r, "before", None) is not None]
    if not records:
        return verdict
    summary = _summary(ctx)
    reasons: list[str] = []
    for record in records:
        payload = await _call(
            record,
            "before_tool",
            record.before,
            (record.api, _copy(record.config), dict(summary), tool.name, _copy(args)),
        )
        if payload is None:
            continue
        name = str(getattr(record, "name", "?"))
        denied = bool(payload.get("deny")) or str(payload.get("decision") or "").strip().lower() == "deny"
        if denied:
            log.warning("hooks: %s before_tool tried to deny %s; a hook may only ask, so this is an ask", name, tool.name)
        if bool(payload.get("ask")) or denied:
            verdict.ask = True
            reason = str(payload.get("reason") or "").strip() or f"{tool.name} needs approval"
            reasons.append(f"{name}: {reason}")
        verdict.notes += _notes(name, payload)
    verdict.reason = "; ".join(reasons)
    return verdict


async def after(app: Any, ctx: ToolContext, tool: Tool, args: dict[str, Any], result: ToolResult) -> ToolResult:
    """Consult every enabled connector's after-hook; append `ctx.meta["hook_notes"]`
    (left there by `before`) and any after-notes to the result.

    The result is never swallowed, and `ok` / `meta` are never changed on a hook's
    say-so: annotating the content is the whole power."""
    meta = getattr(ctx, "meta", None) or {}
    stored = meta.get("hook_notes")
    notes = [str(n) for n in stored if str(n).strip()] if isinstance(stored, (list, tuple)) else []
    records = [r for r in _records(app) if getattr(r, "after", None) is not None]
    if records:
        summary = _summary(ctx)
        seen = _result_summary(result)
        for record in records:
            payload = await _call(
                record,
                "after_tool",
                record.after,
                (record.api, _copy(record.config), dict(summary), tool.name, _copy(args), _copy(seen)),
            )
            if payload is not None:
                notes += _notes(str(getattr(record, "name", "?")), payload)
    if notes:
        content = result.content if isinstance(result.content, str) else str(result.content)
        result.content = content + ("\n" if content else "") + "\n".join(notes)
    return result
