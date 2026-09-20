"""Permission endpoints: tool policies, allowed roots, protected paths (03 §12.1, §13.2).

Every permission the dispatcher consults is editable here, and every edit emits
`settings.changed` so an open Settings screen and a running turn never disagree.

Removing a protected path is itself an `ask` (03 §15.3): the endpoint does not delete
anything, it creates an approval on the desk and answers `202` with the row. The user
approves it on the same card as any other permission question, and
`approvals.decide(allow)` performs the removal. There is no second path that quietly
removes a protection.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

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


@router.post("/protected-paths")
async def add_protected(body: dict[str, Any] = Body(...), app: App = Depends(get_app)) -> dict[str, Any]:
    path = str((body or {}).get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    return {"path": fs_guard.add_protected(app, path)}


@router.delete("/protected-paths", status_code=202)
async def remove_protected(path: str, app: App = Depends(get_app)) -> dict[str, Any]:
    """Not a deletion: an approval. 202 with the pending row; the path goes when the
    user allows it."""
    normalised = fs_guard.normalise(path)
    row = app.db.execute("SELECT 1 FROM protected_paths WHERE path=?", (normalised,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"not a protected path: {normalised}")
    desk_id = app.desk_id()
    conversation = app.conversation(desk_id) or {"id": desk_id}
    call = ToolCall(
        id=f"protected_paths_remove:{normalised}",
        name=approvals_mod.INTERNAL_TOOL,
        arguments={"path": normalised},
    )
    ctx = ToolContext(app=app, conversation=conversation, call_id=call.id, request_text="remove a protected path")
    approval_id = approvals_mod.create(
        app,
        ctx,
        call,
        reason=f"remove {normalised} from the protected paths",
        options={"policy_key": approvals_mod.INTERNAL_TOOL},
    )
    return {"approval": approvals_mod.get(app, approval_id), "path": normalised}
