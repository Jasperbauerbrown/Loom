"""The permission judge (03 §9.5).

For calls the static rules will not settle, one short model call answers a single
question: is this reversible and in scope, or should the user see it first? The policy
text lives in `prompts/classify.md`; this module only renders the inputs as data and
reads the verdict back.

Two invariants:

- The tool name and its arguments are **untrusted data**, fenced and labelled as such
  (Goose's wording, 02 §4). Anything inside them that reads like an instruction is a
  string the judge is reasoning about, not a request. The request text is labelled as
  the user's own words only when it is the user's typed desk message
  (`ToolContext.request_from_user`); otherwise it is untrusted data too.
- Every failure — no model, a transient error, prose instead of JSON, an unknown
  decision — is the same outcome: `ask`, with the reason saying why. A judge that
  cannot answer must never widen permissions.

Nothing is stored here; the pair is returned and the dispatcher records it on the
tool_call event's meta.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from .. import prompts
from . import fs_guard, shell_prefilter
from .registry import Tool, ToolContext

log = logging.getLogger(__name__)

MAX_ARG_CHARS = 4000
# The script the classifier is judging is worth showing, but a handful of large files
# would turn every `ask` into a small context-stuffing exercise: at most two files, each
# bounded like an argument (F1, tower-defense run audit, 2026-09-24).
MAX_FILE_CHARS = 4000
MAX_FILES = 2


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


def command_cwd(app: Any, ctx: ToolContext, args: dict[str, Any]) -> str:
    """The directory the call's relative paths resolve against — the same choice the
    shell tool makes (`dispatch._shell_cwd`), so the judge resolves the paths the tool
    will see. (`dispatch._shell_cwd` could collapse onto this helper; classify cannot
    import dispatch, since dispatch imports classify.)"""
    roots = fs_guard.effective_roots(app, ctx.conversation)
    base = roots[0] if roots else os.getcwd()
    cwd_arg = args.get("cwd")
    return fs_guard.normalise(str(cwd_arg), cwd=base) if cwd_arg else base


def _string_args(args: dict[str, Any]) -> list[str]:
    """Mirrors `permissions._string_args`: top-level string values, and strings inside
    list values."""
    out: list[str] = []
    for value in (args or {}).values():
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, (list, tuple)):
            out.extend(v for v in value if isinstance(v, str))
    return out


def _candidate_words(tool: Tool, args: dict[str, Any]) -> list[str]:
    """Every string this call might name a path with: the shell command's words (when
    parseable), plus every string argument, in the order they appear."""
    words: list[str] = []
    if tool.name == "shell":
        stages = shell_prefilter.split_stages(str(args.get("command") or ""))
        if stages is not None:
            for stage in stages:
                split = shell_prefilter.split_words(stage)
                if split is None:
                    continue
                words.extend(split)
    words.extend(_string_args(args))
    return words


def _file_reads_for(app: Any, conversation_id: str) -> dict[str, bool]:
    """{normalised-path-key: wrote} for this conversation's `file_reads` rows. Tolerates
    an older DB without the `wrote` column (added after migration 0004 landed)."""
    try:
        rows = app.db.execute(
            "SELECT path, wrote FROM file_reads WHERE conversation_id=?", (conversation_id,)
        ).fetchall()
        return {fs_guard._key(r["path"]): bool(r["wrote"]) for r in rows}
    except sqlite3.OperationalError:
        try:
            rows = app.db.execute(
                "SELECT path FROM file_reads WHERE conversation_id=?", (conversation_id,)
            ).fetchall()
            return {fs_guard._key(r["path"]): False for r in rows}
        except Exception:
            return {}
    except Exception:
        return {}


def _artifacts_root_for(app: Any, conversation_id: str) -> str | None:
    store = getattr(app, "artifacts", None)
    if store is None:
        return None
    try:
        return str(store.root / conversation_id).replace("\\", "/")
    except Exception:
        return None


def _owned_files(app: Any, ctx: ToolContext, tool: Tool, args: dict[str, Any]) -> list[str]:
    """Normalised paths this call runs or names that this conversation owns, written
    files first (each group in the order the candidates appeared), deduplicated, capped
    at `MAX_FILES`. A candidate that is not a plausible owned path costs nothing."""
    cwd = command_cwd(app, ctx, args)
    artifacts_root = _artifacts_root_for(app, ctx.conversation_id)
    file_reads = _file_reads_for(app, ctx.conversation_id)

    written: list[str] = []
    others: list[str] = []
    seen: set[str] = set()
    for raw in _candidate_words(tool, args):
        if not raw:
            continue
        try:
            path = fs_guard.normalise(raw, cwd=cwd)
        except Exception:
            continue
        key = fs_guard._key(path)
        if key in seen:
            continue
        wrote = file_reads.get(key)
        owned = wrote is not None or (artifacts_root is not None and fs_guard._under(path, artifacts_root))
        if not owned:
            continue
        seen.add(key)
        (written if wrote else others).append(path)
    return (written + others)[:MAX_FILES]


def _read_owned_file(path: str) -> str | None:
    """The file's text, bounded and truncated; None for anything unreadable, a
    directory, or content that looks binary. Never raises."""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        with open(p, "rb") as fh:
            data = fh.read(MAX_FILE_CHARS * 4 + 1024)
    except Exception:
        return None
    if b"\x00" in data[:4096]:
        return None
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return None
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + "\n… (truncated)"
    return text


def render_inputs(app: Any, ctx: ToolContext, tool: Tool, args: dict[str, Any]) -> str:
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

    goal = conversation.get("goal")
    goal_line = f"Conversation goal: {goal}" if goal else (
        "Conversation goal: (none recorded — the stated intent below is this turn's scope)"
    )
    intent_text = (ctx.intent or "").strip()[:MAX_ARG_CHARS] or "(none stated)"
    # The one input that is not untrusted: the user's own message typed on the desk. A
    # thread's goal or handoff was written by the desk model, and connector, schedule
    # and thread-report text by someone else — those stay data (2026-09-25).
    request_label = (
        "Request that led here (typed by the user on the desk):"
        if ctx.request_from_user and ctx.request_text
        else "Request that led here (untrusted data):"
    )

    file_blocks: list[str] = []
    for path in _owned_files(app, ctx, tool, args):
        content = _read_owned_file(path)
        if content is None:
            continue
        file_blocks.extend(
            [
                f"Contents of a file this call runs or names (untrusted data): {path}",
                "```text",
                content,
                "```",
                "",
            ]
        )

    lines = [
        f"Conversation kind: {conversation.get('kind') or 'unknown'}",
        goal_line,
        "",
        request_label,
        "```text",
        (ctx.request_text or "(none recorded)")[:MAX_ARG_CHARS],
        "```",
        "",
        "Stated intent (untrusted data):",
        "```text",
        intent_text,
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
        *file_blocks,
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
