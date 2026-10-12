"""Permission endpoints: tool policies, allowed roots, protected paths (03 §12.1, §13.2).

Every permission the dispatcher consults is editable here, and every edit emits
`settings.changed` so an open Settings screen and a running turn never disagree.

Removing a protected path is itself an `ask` (03 §15.3): the endpoint does not delete
anything, it creates an approval on the desk and answers `202` with the row. The user
approves it on the same card as any other permission question, and
`approvals.decide(allow)` performs the removal. There is no second path that quietly
removes a protection. Weakening one — `all` to `writes` (2026-10-12) — rides the same
road; strengthening one, or adding one, takes effect at once.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from ...app import App
from ...models.types import ToolCall
from ...tools import approvals as approvals_mod
from ...tools import fs_guard, permissions
from ...tools.registry import ToolContext
from ..deps import get_app

router = APIRouter(tags=["permissions"])


# ---------------------------------------------------------------------------------
# tool policies
# ---------------------------------------------------------------------------------
@router.get("/tool-policies")
async def list_tool_policies(app: App = Depends(get_app)) -> list[dict[str, Any]]:
    return permissions.list_policies(app)


@router.put("/tool-policies/{tool:path}")
async def set_tool_policy(
    tool: str, body: dict[str, Any] = Body(...), app: App = Depends(get_app)
) -> dict[str, Any]:
    mode = str((body or {}).get("mode") or "")
    if mode not in permissions.MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {list(permissions.MODES)}")
    if permissions.is_path_class(tool):
        base, prefix = permissions.split_key(tool)
        if not base or not prefix:
            raise HTTPException(status_code=400, detail="a path-class key looks like 'knowledge_write:core/'")
    elif app.tools.get(tool) is None:
        raise HTTPException(status_code=404, detail=f"unknown tool {tool!r}")
    permissions.set_policy(app, tool, mode)
    return {"tool": tool, "mode": mode}


@router.delete("/tool-policies/{tool:path}")
async def clear_tool_policy(tool: str, app: App = Depends(get_app)) -> dict[str, Any]:
    if not permissions.clear_policy(app, tool):
        raise HTTPException(status_code=404, detail=f"no policy row for {tool!r}")
    registered = app.tools.get(tool)
    return {
        "tool": tool,
        "mode": permissions.default_mode(registered) if registered is not None else None,
        "cleared": True,
    }


# ---------------------------------------------------------------------------------
# roots
# ---------------------------------------------------------------------------------
@router.get("/roots")
async def list_roots(app: App = Depends(get_app)) -> list[dict[str, Any]]:
    return [dict(r) for r in app.db.execute("SELECT * FROM roots ORDER BY path")]


@router.post("/roots")
async def add_root(body: dict[str, Any] = Body(...), app: App = Depends(get_app)) -> dict[str, Any]:
    path = str((body or {}).get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    stored = fs_guard.add_root(app, path, added_by="user")
    row = app.db.execute("SELECT * FROM roots WHERE path=?", (stored,)).fetchone()
    return dict(row) if row else {"path": stored}


@router.delete("/roots")
async def remove_root(path: str, app: App = Depends(get_app)) -> dict[str, Any]:
    if not fs_guard.remove_root(app, path):
        raise HTTPException(status_code=404, detail=f"not a root: {path}")
    return {"path": fs_guard.normalise(path), "removed": True}


# ---------------------------------------------------------------------------------
# protected paths
# ---------------------------------------------------------------------------------
@router.get("/protected-paths")
async def list_protected(app: App = Depends(get_app)) -> list[dict[str, Any]]:
    return [dict(r) for r in app.db.execute("SELECT * FROM protected_paths ORDER BY path")]


def _mode(body: dict[str, Any] | None, default: str | None = "all") -> str:
    mode = (body or {}).get("mode") or default
    if mode not in fs_guard.MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {list(fs_guard.MODES)}")
    return str(mode)


def _protected_row(app: App, path: str) -> dict[str, Any]:
    row = app.db.execute("SELECT * FROM protected_paths WHERE path=?", (path,)).fetchone()
    return dict(row) if row else {"path": path}


@router.post("/protected-paths")
async def add_protected(body: dict[str, Any] = Body(...), app: App = Depends(get_app)) -> dict[str, Any]:
    """Add a protected path, `mode` `all` (the default) or `writes`. An existing row
    keeps its mode: changing it is `PUT`, because weakening one is an approval."""
    path = str((body or {}).get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    stored = fs_guard.add_protected(app, path, _mode(body))
    return _protected_row(app, stored)


def _weakening_approval(app: App, path: str, reason: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """The approval that gates removing or weakening a protection (03 §15.3)."""
    desk_id = app.desk_id()
    conversation = app.conversation(desk_id) or {"id": desk_id}
    suffix = f":{arguments['mode']}" if arguments.get("mode") else ""
    call = ToolCall(
        id=f"protected_paths_remove:{path}{suffix}",
        name=approvals_mod.INTERNAL_TOOL,
        arguments=arguments,
    )
    ctx = ToolContext(app=app, conversation=conversation, call_id=call.id, request_text=reason)
    approval_id = approvals_mod.create(
        app,
        ctx,
        call,
        reason=reason,
        options={"policy_key": approvals_mod.INTERNAL_TOOL},
    )
    return {"approval": approvals_mod.get(app, approval_id), "path": path}


@router.put("/protected-paths")
async def set_protected_mode(body: dict[str, Any] = Body(...), app: App = Depends(get_app)) -> Any:
    """Change a row's mode. `writes` → `all` applies at once (200 with the row);
    `all` → `writes` weakens it, so it is an approval (202, like a removal)."""
    raw = str((body or {}).get("path") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="path is required")
    mode = _mode(body, default=None)
    normalised = fs_guard.normalise(raw)
    current = fs_guard.protected_mode(app, normalised)
    if current is None:
        raise HTTPException(status_code=404, detail=f"not a protected path: {normalised}")
    if current != mode and mode == "all":
        fs_guard.set_protected_mode(app, normalised, mode)
    if current == mode or mode == "all":
        return _protected_row(app, normalised)
    return JSONResponse(
        status_code=202,
        content=_weakening_approval(
            app,
            normalised,
            f"protect {normalised} against writes only (reading and running it would no longer ask)",
            {"path": normalised, "mode": mode},
        ),
    )


@router.delete("/protected-paths", status_code=202)
async def remove_protected(path: str, app: App = Depends(get_app)) -> dict[str, Any]:
    """Not a deletion: an approval. 202 with the pending row; the path goes when the
    user allows it."""
    normalised = fs_guard.normalise(path)
    if fs_guard.protected_mode(app, normalised) is None:
        raise HTTPException(status_code=404, detail=f"not a protected path: {normalised}")
    return _weakening_approval(app, normalised, f"remove {normalised} from the protected paths", {"path": normalised})
