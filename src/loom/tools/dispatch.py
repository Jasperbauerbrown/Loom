"""The dispatcher: the only path from a model's tool call to execution (03 §9.3).

```
1 lookup tool          unknown          → error result
2 validate arguments   malformed        → error result ("resend the call")
3 filesystem guard     outside/protected→ approval
4 policy               allow | ask → approval | classify → pre-filter, then the judge
                       (desk auto-approve: ask/classify/outside-roots → allow; protected still asks;
                        a conversation grant — "allow this call for the conversation" — allows)
4½ before-hooks        connectors may annotate, or turn the allow into an ask; never deny
5 secrets substitution into a private copy of the arguments
6 execute with a timeout; every exception becomes an error result
7 redact the output, bound it (spill to an artifact over the cap); after-hooks annotate
```

Nothing else may call a handler. Every step failure has exactly one behaviour: an error
result the model can read and act on, or an approval the user answers. `dispatch` never
raises — a tool that explodes is an input, not an incident.

`execute` is steps 5–7 alone: the runner calls it when an approval comes back allowed,
so an approved call is not re-judged.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .. import prompts
from ..artifacts import sniff_image
from ..models.types import ToolCall
from ..secrets import SecretMissing
from . import approvals, classify, fs_guard, hooks, permissions, shell_prefilter
from .registry import Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0


@dataclass
class ApprovalNeeded:
    """The turn stops here until the user answers (03 §6.3)."""

    approval_id: str
    reason: str
    options: dict[str, Any] = field(default_factory=dict)


def denied_result(reason: str = "denied by user") -> ToolResult:
    """What the model sees when an approval comes back denied."""
    return ToolResult(content=f"denied: {reason}", ok=False, meta={"denied": True})


AUTO_REASON = "desk auto-approve"


def _auto_approved(app: Any, ctx: ToolContext, tool: Tool, args: dict[str, Any]) -> bool:
    """Desk auto-approve (`permissions.desk_auto_approve`): a desk call that would ask
    or classify runs instead. Grant-only, like the pre-filter. Protected paths keep
    asking: the fs guard's own ask, and a shell command that names one (or cannot be
    parsed to tell) takes the ordinary path."""
    if (ctx.conversation or {}).get("kind") != "desk":
        return False
    if not app.settings.get("permissions.desk_auto_approve"):
        return False
    if tool.name == "shell":
        stages = shell_prefilter.split_stages(str(args.get("command") or ""))
        if stages is None:
            return False
        words: list[str] = []
        for stage in stages:
            split = shell_prefilter.split_words(stage)
            if split is None:
                return False
            words += split
        if shell_prefilter.names_protected(words, fs_guard.protected_paths(app), classify.command_cwd(app, ctx, args)):
            return False
    return True


# ---------------------------------------------------------------------------------
# Step 2: argument validation (a minimal JSON-schema check; unknown keys are fine)
# ---------------------------------------------------------------------------------
_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def _type_ok(value: Any, expected: str) -> bool:
    types = _TYPES.get(expected)
    if types is None:
        return True
    if expected in ("integer", "number") and isinstance(value, bool):
        return False  # bools are ints in Python; a model meaning true never means 1 here
    return isinstance(value, types)


def validate_args(tool: Tool, args: dict[str, Any]) -> list[str]:
    """Human-readable problems with the call's arguments; empty means usable."""
    errors: list[str] = []
    if not isinstance(args, dict):
        return [f"arguments must be a JSON object, got {type(args).__name__}"]
    schema = tool.parameters or {}
    properties = schema.get("properties") or {}
    for name in schema.get("required") or []:
        if args.get(name) is None:
            errors.append(f"missing required argument {name!r}")
    for name, value in args.items():
        spec = properties.get(name)
        if not isinstance(spec, dict) or value is None:
            continue
        expected = spec.get("type")
        if isinstance(expected, str) and not _type_ok(value, expected):
            errors.append(f"{name} must be {expected}, got {type(value).__name__}")
            continue
        if isinstance(expected, list) and not any(_type_ok(value, e) for e in expected if isinstance(e, str)):
            errors.append(f"{name} must be one of {expected}, got {type(value).__name__}")
            continue
        choices = spec.get("enum")
        if isinstance(choices, list) and choices and value not in choices:
            errors.append(f"{name} must be one of {choices}, got {value!r}")
    return errors


# ---------------------------------------------------------------------------------
# Step 6: timeouts
# ---------------------------------------------------------------------------------
def timeout_for(app: Any, tool: Tool, args: dict[str, Any] | None = None) -> float:
    """The timeout for one call. A tool that declares a `timeout_s` argument (shell,
    http) gets the value the model asked for, clamped by
    `conversation.tool_timeout_max_s`; otherwise the tool's own or the per-kind setting."""
    if "timeout_s" in ((tool.parameters or {}).get("properties") or {}):
        try:
            asked = 0.0 if isinstance((args or {}).get("timeout_s"), bool) else float((args or {}).get("timeout_s"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            asked = 0.0  # junk is not an error: the default stands
        if asked > 0:
            return min(asked, float(app.settings.get("conversation.tool_timeout_max_s")))
    if tool.timeout_s:
        return float(tool.timeout_s)
    if tool.name == "shell":
        return float(app.settings.get("conversation.tool_timeout_shell_s"))
    if tool.name == "http":
        return float(app.settings.get("conversation.tool_timeout_http_s"))
    if tool.source.startswith("mcp:"):
        return float(app.settings.get("conversation.tool_timeout_mcp_s"))
    return DEFAULT_TIMEOUT_S


# ---------------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------------
async def dispatch(app: Any, ctx: ToolContext, call: ToolCall) -> ToolResult | ApprovalNeeded:
    tool = app.tools.get(call.name)
    if tool is None:
        available = ", ".join(app.tools.names()) or "(none registered)"
        return ToolResult.error(f"unknown tool {call.name}; available: {available}")

    args = call.arguments if isinstance(call.arguments, dict) else {}
    problems = [call.parse_error] if call.parse_error else []
    problems += validate_args(tool, args)
    if problems:
        return ToolResult.error("invalid arguments: " + "; ".join(str(p) for p in problems) + "; resend the call")

    guard = fs_guard.check(app, ctx.conversation, tool, args)
    if isinstance(guard, fs_guard.GuardAsk) and not guard.protected and _auto_approved(app, ctx, tool, args):
        ctx.meta.update({"policy": "auto", "decision": "allow", "reason": AUTO_REASON})
        return await _run(app, ctx, call, tool, args)
    if isinstance(guard, fs_guard.GuardAsk):
        ctx.meta.update({"policy": "ask", "decision": "ask", "reason": guard.reason})
        approval_id = approvals.create(app, ctx, call, guard.reason, dict(guard.options))
        return ApprovalNeeded(approval_id, guard.reason, dict(guard.options))

    mode, policy_key = permissions.decide(app, tool, args)
    if mode != "allow" and _auto_approved(app, ctx, tool, args):
        ctx.meta.update({"policy": "auto", "decision": "allow", "reason": AUTO_REASON})
        return await _run(app, ctx, call, tool, args, policy_key)
    if mode != "allow":
        granted = permissions.conversation_grant(app, ctx.conversation_id, tool.name, args)
        if granted:
            ctx.meta.update({"policy": mode, "decision": "allow", "reason": granted})
            return await _run(app, ctx, call, tool, args, policy_key)
    reason = ""
    if mode == "classify":
        if tool.name == "shell" and shell_prefilter.is_readonly(
            str(args.get("command") or ""),
            list(app.settings.get("permissions.shell_readonly_commands") or []),
            protected=fs_guard.protected_paths(app),
            cwd=classify.command_cwd(app, ctx, args),
        ):
            decision, reason = "allow", "read-only command (static pre-filter)"
        else:
            decision, reason = await classify.judge(app, ctx, tool, args)
    else:
        decision = mode
    ctx.meta.update({"policy": mode, "decision": decision, "reason": reason})

    if decision == "ask":
        ask_reason = reason or f"{tool.name} requires approval"
        options = {"policy_key": policy_key}
        approval_id = approvals.create(app, ctx, call, ask_reason, options)
        return ApprovalNeeded(approval_id, ask_reason, options)

    return await _run(app, ctx, call, tool, args, policy_key)


async def _run(app: Any, ctx: ToolContext, call: ToolCall, tool: Tool, args: dict[str, Any], policy_key: str | None = None) -> ToolResult | ApprovalNeeded:
    """Step 4½: the call is allowed; connector before-hooks may still turn it into an
    ask or annotate it. Then steps 5–7."""
    verdict = await hooks.before(app, ctx, tool, args)
    if verdict.ask:
        ask_reason = verdict.reason or f"{tool.name} requires approval (connector hook)"
        ctx.meta.update({"decision": "ask", "reason": ask_reason})
        options = {"policy_key": policy_key} if policy_key else {}
        approval_id = approvals.create(app, ctx, call, ask_reason, options)
        return ApprovalNeeded(approval_id, ask_reason, options)
    if verdict.notes:
        ctx.meta["hook_notes"] = list(verdict.notes)
    return await execute(app, ctx, tool, args)


def _store_images(app: Any, ctx: ToolContext, result: ToolResult, content: str) -> str:
    refs: list[dict[str, Any]] = list(result.meta.get("images") or [])
    notes: list[str] = []
    for image in result.images or []:
        data = image.get("data") if isinstance(image, dict) else None
        mime = str((image.get("mime") if isinstance(image, dict) else "") or "") or (sniff_image(data) if isinstance(data, (bytes, bytearray)) else "")
        if app.artifacts is None:
            notes.append("[image not stored: no artifact store]")
            continue
        try:
            if not isinstance(data, (bytes, bytearray)):
                raise ValueError("image data is not bytes")
            ref = app.artifacts.store_image(ctx.conversation_id, bytes(data), mime or "")
        except (ValueError, OSError) as exc:
            notes.append(f"[image not stored: {exc}]")
            continue
        refs.append({"id": ref["id"], "mime": ref["mime"]})
        notes.append(prompts.load("context/tool-image-marker").replace("{id}", ref["id"]))
    result.images = None
    if refs:
        result.meta["images"] = refs
    if notes:
        content = content + ("\n" if content else "") + "\n".join(notes)
    return content


async def execute(app: Any, ctx: ToolContext, tool: Tool, args: dict[str, Any]) -> ToolResult:
    """Steps 5–7. Used by `dispatch` and, after an approval, by the runner."""
    private_args = args
    if app.secrets is not None:
        try:
            private_args = app.secrets.substitute(args)
        except SecretMissing as exc:
            return ToolResult.error(
                f"no secret named {exc.name}; store it with secret_set or in Settings → Integrations"
            )
        except Exception as exc:  # a broken secrets backend must not raise into the turn
            return ToolResult.error(f"secret substitution failed: {exc}")

    timeout = timeout_for(app, tool, private_args)
    try:
        # A grace margin above the effective timeout, so a tool that bounds itself (shell
        # kills its tree and returns what the command said first) gets to answer and this
        # catches only a handler that ignored its own deadline. The message still names
        # the effective timeout, which is the number the caller asked for.
        result = await asyncio.wait_for(
            tool.handler(ctx, private_args), timeout=timeout + min(30.0, max(1.0, 0.1 * timeout))
        )
    except asyncio.TimeoutError:
        result = ToolResult.error(f"{tool.name} timed out after {timeout:g}s", timeout=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("tool %s raised: %r", tool.name, exc, exc_info=exc)
        result = ToolResult.error(f"{tool.name} failed: {type(exc).__name__}: {exc}")
    if not isinstance(result, ToolResult):
        result = ToolResult.error(f"{tool.name} returned {type(result).__name__}, not a ToolResult")

    content = result.content if isinstance(result.content, str) else str(result.content)
    if result.images:
        # Store first, reference second: each image becomes an artifact file and a
        # `{id, mime}` ref in meta; the text gets one marker line per image so the model
        # can name it later. Bytes never pass redaction or bounding (03 §4.4).
        content = _store_images(app, ctx, result, content)
    if app.secrets is not None:
        try:
            content = app.secrets.redact(content)
        except Exception as exc:
            log.warning("redaction failed: %r", exc)
    if app.artifacts is not None and not tool.paged:
        # A paged tool (fs_read, artifact_recall) already sized its page to the cap; its
        # footer rides on top. Spilling it would re-create the artifact it was reading
        # and the model could never get past the first page (2026-09-22).
        content, artifact_id = app.artifacts.bound(ctx.conversation_id, ctx.event_id, content)
        if artifact_id:
            result.meta["artifact_id"] = artifact_id
    result.content = content
    try:
        result = await hooks.after(app, ctx, tool, args, result)
    except Exception as exc:  # a hook may annotate, never break the call
        log.warning("after-hook raised: %r", exc)
    return result
