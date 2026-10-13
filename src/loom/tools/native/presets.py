"""`presets_list`: the read-only model-preset enumerator (03 §9.2).

Agents choosing a preset for `conversation_create` read the `presets` and
`model_roles` tables of state.db directly — the same access pattern as the
conversation tools' SELECTs. Deliberately no HTTP: Loom never interacts with its
own API as part of normal operation (charter decision log, 2026-10-13), so this
tool replaces the `GET /api/presets` pointer conversation_create's prompt used
to suggest.
"""

from __future__ import annotations

import json
from typing import Any

from ... import db, prompts
from ..registry import Tool, ToolContext, ToolResult

# The params an agent weighs when picking a preset, in the order shown; anything else
# the preset sets follows on the same line, sorted, so nothing is hidden.
SHOW_PARAMS = ("effective_ctx", "max_tokens", "reasoning", "multimodal")


async def _list(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    app = ctx.app
    rows = app.db.execute(
        "SELECT name, provider, model, params, notes FROM presets WHERE enabled ORDER BY name"
    ).fetchall()
    lines = ["enabled presets:", "name | provider | model | params | notes"]
    for r in rows:
        params = db.loads(r["params"], {}) or {}
        keys = [k for k in SHOW_PARAMS if k in params]
        keys += sorted(k for k in params if k not in SHOW_PARAMS)
        shown = ", ".join(f"{k}={json.dumps(params[k])}" for k in keys) or "-"
        lines.append(f"{r['name']} | {r['provider']} | {r['model']} | {shown} | {r['notes'] or ''}")

    lines += ["", "role bindings:"]
    bound = app.db.execute(
        "SELECT mr.role, mr.preset, p.enabled FROM model_roles mr "
        "LEFT JOIN presets p ON p.name = mr.preset ORDER BY mr.role"
    ).fetchall()
    for r in bound:
        preset = r["preset"]
        if not preset:
            lines.append(f"{r['role']} → (unset)")
        elif not r["enabled"]:
            lines.append(f"{r['role']} → {preset} (not enabled)")
        else:
            lines.append(f"{r['role']} → {preset}")
    return ToolResult("\n".join(lines), meta={"count": len(rows)})


def tools() -> list[Tool]:
    return [
        Tool(
            name="presets_list",
            description=prompts.load("tools/presets_list"),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_list,
            read_only=True,
        ),
    ]