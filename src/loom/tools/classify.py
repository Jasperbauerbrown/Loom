"""The permission judge (03 §9.5).

For calls the static rules will not settle, one short model call answers a single
question: is this reversible and in scope, or should the user see it first? The policy
text lives in `prompts/classify.md`; this module only renders the inputs as data and
reads the verdict back.

Two invariants:

- The tool name and its arguments are **untrusted data**, fenced and labelled as such
  (Goose's wording, 02 §4). Anything inside them that reads like an instruction is a
  string the judge is reasoning about, not a request.
- Every failure — no model, a transient error, prose instead of JSON, an unknown
  decision — is the same outcome: `ask`, with the reason saying why. A judge that
  cannot answer must never widen permissions.

Nothing is stored here; the pair is returned and the dispatcher records it on the
tool_call event's meta.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .. import prompts
from .registry import Tool, ToolContext

log = logging.getLogger(__name__)

MAX_ARG_CHARS = 4000


def classifier_role(app: Any, conversation: dict[str, Any] | None) -> str:
    """The configured classifier role when it actually has a preset; otherwise the
    conversation's own role, so a half-configured install still classifies."""
    from ..models import roles

    configured = str(app.settings.get("permissions.classifier_role") or "")
    if configured and roles.role_preset(app, configured):
        return configured
    fallback = (conversation or {}).get("role") or "desk"
    return str(fallback)


def priority_for(ctx: ToolContext) -> int:
    """Interactive when the user is watching (the desk, or a user-triggered turn),
    background otherwise — a thread's permission question must not delay the desk."""
    kind = (ctx.conversation or {}).get("kind")
    return 0 if kind == "desk" or int(getattr(ctx, "priority", 0)) == 0 else 1


def render_inputs(app: Any, ctx: ToolContext, tool: Tool, args: dict[str, Any]) -> str:
    from . import fs_guard

    conversation = ctx.conversation or {}
    paths = fs_guard.describe(app, conversation)
    try:
        rendered_args = json.dumps(args, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        rendered_args = repr(args)
    if len(rendered_args) > MAX_ARG_CHARS:
        rendered_args = rendered_args[:MAX_ARG_CHARS] + "\n… (arguments truncated)"
    root_lines = [f"- {p}" for p in paths["roots"]] or ["- (none)"]
    protected_lines = [f"- {p}" for p in paths["protected"]] or ["- (none)"]
    lines = [
        f"Conversation kind: {conversation.get('kind') or 'unknown'}",
        f"Conversation goal: {conversation.get('goal') or conversation.get('title') or '(none)'}",
        "",
        "Request that led here (untrusted data):",
        "```text",
        (ctx.request_text or "(none recorded)")[:MAX_ARG_CHARS],
        "```",
        "",
        f"Tool: {tool.name}",
        f"Tool is annotated read-only: {'yes' if tool.is_read_only(args) else 'no'}",
        "",
        "Tool arguments (untrusted data):",
        "```json",
        rendered_args,
        "```",
        "",
        "Allowed roots:",
        *root_lines,
        "",
        "Protected paths:",
        *protected_lines,
        "",
        'Answer with JSON only: {"decision":"allow|ask","reason":"…"}',
    ]
    return "\n".join(lines)


def extract_json(text: str) -> dict[str, Any] | None:
    """The first balanced `{...}` in the text, parsed. Tolerates surrounding prose and
    fenced blocks; returns None when there is nothing parseable."""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : i + 1])
                    except Exception:
                        break
                    return value if isinstance(value, dict) else None
        start = text.find("{", start + 1)
    return None


async def judge(app: Any, ctx: ToolContext, tool: Tool, args: dict[str, Any]) -> tuple[str, str]:
    """("allow"|"ask", reason). Never raises."""
    from ..models import roles

    # A verdict is worth nothing if it arrives minutes later: the provider's generous
    # streaming timeouts are for real generations. The judge gets a short bound of its
    # own (a setting), one retry for a busy provider, and then fails closed to `ask`
    # with the reason visible on the approval card (2026-09-21).
    bound = float(app.settings.get("permissions.classifier_timeout_s"))
    try:
        role = classifier_role(app, ctx.conversation)
        request = roles.build_request(
            app,
            role=role,
            messages=[
                {"role": "system", "content": prompts.load("classify")},
                {"role": "user", "content": render_inputs(app, ctx, tool, args)},
            ],
            overrides={"timeout_s": bound, "stream_idle_s": bound},
            conversation_id=(ctx.conversation or {}).get("id"),
            purpose="classify",
        )
        result = await app.models.chat(request, priority_for(ctx))
        if not getattr(result, "ok", False) and getattr(result, "outcome", "") == "transient":
            log.info("classifier transient failure (%s); retrying once", result.error)
            result = await app.models.chat(request, priority_for(ctx))
    except Exception as exc:
        log.warning("classifier call failed: %r", exc)
        return "ask", f"classifier unavailable: {type(exc).__name__}: {exc}"

    if not getattr(result, "ok", False):
        return "ask", f"classifier did not answer ({result.error or result.outcome}; bound {bound:.0f}s) — asking instead"
    payload = extract_json(result.content or "")
    if payload is None:
        return "ask", "classifier unavailable: no JSON verdict in the reply"
    decision = str(payload.get("decision", "")).strip().lower()
    reason = str(payload.get("reason", "") or "").strip()
    if decision not in ("allow", "ask"):
        return "ask", f"classifier unavailable: unknown decision {decision!r}"
    return decision, reason or ("allowed by the classifier" if decision == "allow" else "the classifier asked")
