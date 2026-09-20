"""Which mode governs a call: step 4 of the dispatcher pipeline (03 §9.2, §9.3).

Three modes. `allow` runs it, `ask` persists an approval and stops the turn, `classify`
hands the judgement to a model (after the shell pre-filter has had its chance to grant).

Resolution order for one call:

1. **Path-class rows.** A `tool_policies` row keyed `<tool>:<prefix>` (e.g.
   `knowledge_write:core/`) applies when any string argument of the call starts with
   `<prefix>`. The longest matching prefix wins. Path classes are checked first because
   they are the more specific statement about the same tool.
2. **The tool's row.** `tool_policies[<tool>]`, as bootstrapped or edited in Settings.
3. **No row at all** (MCP and connector tools, which are registered long after the
   bootstrap and never get rows written for them): read-only → `allow`, else `ask`.

One grant-only shortcut sits on top, mirroring the shell pre-filter: a call that is
read-only *for these arguments* (`Tool.read_only` or `Tool.read_only_for(args)`, e.g.
an `http` GET) turns a matching `classify` row into `allow`. Asking a model whether a
GET is reversible spends a generation to reach a foregone conclusion. It only ever
relaxes `classify`, never an explicit `ask`, and nothing static can turn a row into a
denial.

`policy_key` — the row that decided — travels with an approval so `allow_always`
updates the rule that actually asked, not a broader one.
"""

from __future__ import annotations

from typing import Any

from .. import db
from .registry import Tool

MODES = ("allow", "ask", "classify")


def is_path_class(key: str) -> bool:
    return ":" in key


def split_key(key: str) -> tuple[str, str]:
    """`knowledge_write:core/` → ("knowledge_write", "core/")."""
    tool, _, prefix = key.partition(":")
    return tool, prefix


def rows(app: Any) -> dict[str, str]:
    return {r["tool"]: r["mode"] for r in app.db.execute("SELECT tool, mode FROM tool_policies")}


def row_mode(app: Any, key: str) -> str | None:
    row = app.db.execute("SELECT mode FROM tool_policies WHERE tool=?", (key,)).fetchone()
    return row["mode"] if row else None


def default_mode(tool: Tool) -> str:
    """The mode a tool has when no row exists: read-only is safe, everything else asks."""
    return "allow" if tool.read_only else "ask"


def _string_args(args: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for value in (args or {}).values():
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, (list, tuple)):
            out.extend(v for v in value if isinstance(v, str))
    return out


def path_class_match(app: Any, tool_name: str, args: dict[str, Any]) -> tuple[str, str] | None:
    """The longest `<tool>:<prefix>` row whose prefix starts one of the call's string
    arguments, as (key, mode). Vault-relative paths are matched as given; absolute
    paths are matched with forward slashes."""
    candidates = [v.replace("\\", "/") for v in _string_args(args)]
    best: tuple[str, str] | None = None
    best_len = -1
    for key, mode in rows(app).items():
        if not is_path_class(key):
            continue
        name, prefix = split_key(key)
        if name != tool_name or not prefix:
            continue
        if any(value.startswith(prefix) for value in candidates) and len(prefix) > best_len:
            best, best_len = (key, mode), len(prefix)
    return best


def decide(app: Any, tool: Tool, args: dict[str, Any] | None = None) -> tuple[str, str]:
    """(mode, policy_key). `policy_key` names the row that decided, or the tool name
    when the default applied."""
    args = args or {}
    read_only = tool.is_read_only(args)

    match = path_class_match(app, tool.name, args)
    if match is not None:
        key, mode = match
        if mode == "classify" and read_only:
            return "allow", key
        return (mode if mode in MODES else "ask"), key

    mode = row_mode(app, tool.name)
    if mode is not None:
        if mode == "classify" and read_only:
            return "allow", tool.name
        return (mode if mode in MODES else "ask"), tool.name

    return ("allow" if read_only else "ask"), tool.name


def policy_for(app: Any, tool: Tool, args: dict[str, Any] | None = None) -> str:
    return decide(app, tool, args)[0]


def set_policy(app: Any, tool: Tool | str, mode: str) -> str:
    """Upsert a policy row. `tool` may be a Tool, a tool name, or a path-class key."""
    if mode not in MODES:
        raise ValueError(f"unknown policy mode {mode!r}: expected one of {list(MODES)}")
    key = tool.name if isinstance(tool, Tool) else str(tool)
    app.db.execute(
        "INSERT INTO tool_policies(tool, mode, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(tool) DO UPDATE SET mode=excluded.mode, updated_at=excluded.updated_at",
        (key, mode, db.now_iso()),
    )
    app.bus.emit("settings.changed", {"keys": ["tool_policies"]})
    return key


def clear_policy(app: Any, key: str) -> bool:
    """Remove a row so the tool falls back to its default (or a path class disappears)."""
    cur = app.db.execute("DELETE FROM tool_policies WHERE tool=?", (key,))
    if cur.rowcount:
        app.bus.emit("settings.changed", {"keys": ["tool_policies"]})
    return bool(cur.rowcount)


def list_policies(app: Any) -> list[dict[str, Any]]:
    """Every registered tool joined with its row, plus every path-class row, so the
    Settings table can render the whole permission surface grouped by source."""
    stored = rows(app)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tool in app.tools.all():
        mode = stored.get(tool.name)
        out.append(
            {
                "tool": tool.name,
                "kind": "tool",
                "mode": mode if mode in MODES else default_mode(tool),
                "source": tool.source,
                "read_only": tool.read_only,
                "read_only_for": tool.read_only_for is not None,
                "explicit": tool.name in stored,
                "default": default_mode(tool),
                "description": tool.description.split("\n", 1)[0][:200],
            }
        )
        seen.add(tool.name)
    for key, mode in sorted(stored.items()):
        if key in seen:
            continue
        base = split_key(key)[0] if is_path_class(key) else key
        tool = app.tools.get(base)
        out.append(
            {
                "tool": key,
                "kind": "path_class" if is_path_class(key) else "tool",
                "mode": mode,
                "source": tool.source if tool is not None else "unregistered",
                "read_only": bool(tool.read_only) if tool is not None else False,
                "read_only_for": tool.read_only_for is not None if tool is not None else False,
                "explicit": True,
                "default": default_mode(tool) if tool is not None else "ask",
                "description": "",
            }
        )
    out.sort(key=lambda p: (p["source"], p["tool"]))
    return out
