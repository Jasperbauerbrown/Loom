"""Promotion endpoints (03 §12.1, §15). The System screen's half of self-modification.

Everything here goes through the same `promote.request_promotion` the `promote` tool
uses, so a click and a model call cannot reach different conclusions. The failure class
comes back as a `code` and is mapped onto exactly one status: a protected diff that was
not acknowledged is `409` with the file list, a missing supervisor is `503`, a bad ref
or a dirty tree is `400`, a failing suite is `422` with the output.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ... import promote as promote_mod
from ...app import App
from ..deps import get_app

router = APIRouter(tags=["promote"])

STATUS_FOR: dict[str, int] = {
    "no_repo": 400,
    "bad_ref": 400,
    "dirty": 409,
    "protected": 409,
    "tests_failed": 422,
    "no_supervisor": 503,
    "supervisor_error": 502,
}


@router.get("/system/promote/plan")
async def promote_plan(ref: str, app: App = Depends(get_app)) -> dict[str, Any]:
    """What promoting `ref` would change: the commit, the diff, the protected paths."""
    plan = await asyncio.to_thread(promote_mod.plan, app, ref)
    if not plan.ok:
        raise HTTPException(status_code=STATUS_FOR.get(plan.code, 400), detail=plan.error)
    return plan.to_json()


@router.post("/system/promote")
async def promote(body: dict[str, Any] = Body(default={}), app: App = Depends(get_app)) -> dict[str, Any]:
    ref = str((body or {}).get("ref") or "").strip()
    if not ref:
        raise HTTPException(status_code=400, detail="ref is required")
    outcome = await promote_mod.request_promotion(
        app, ref,
        conversation_id=str((body or {}).get("conversation_id") or "") or None,
        acknowledge_protected=bool((body or {}).get("acknowledge_protected")),
    )
    if not outcome["ok"]:
        detail: Any = outcome["message"]
        if outcome["code"] == "protected":
            detail = {
                "message": outcome["message"],
                "protected_files": outcome["plan"]["protected_files"],
                "acknowledge": "repeat with acknowledge_protected: true",
            }
        elif outcome.get("output"):
            detail = {"message": outcome["message"], "output": outcome["output"][-4000:]}
        raise HTTPException(status_code=STATUS_FOR.get(outcome["code"], 400), detail=detail)
    return outcome


@router.post("/system/rollback")
async def rollback(body: dict[str, Any] = Body(default={}), app: App = Depends(get_app)) -> dict[str, Any]:
    """Back to `previous`. The supervisor restores the pre-promotion snapshot when the
    schema moved on, which loses state written since — the UI says so before asking."""
    client = _client(app)
    answer = await client.rollback(str((body or {}).get("conversation_id") or ""))
    if not answer.get("ok"):
        raise HTTPException(status_code=_supervisor_status(answer), detail=answer.get("error"))
    return answer


@router.post("/system/restart")
async def restart(app: App = Depends(get_app)) -> dict[str, Any]:
    answer = await _client(app).restart()
    if not answer.get("ok"):
        raise HTTPException(status_code=_supervisor_status(answer), detail=answer.get("error"))
    return answer


@router.get("/system/supervisor")
async def supervisor_status(app: App = Depends(get_app)) -> dict[str, Any]:
    answer = await _client(app).status()
    if not answer.get("ok"):
        return {"ok": False, "running": False, "error": answer.get("error")}
    return {"running": True, **answer}


@router.get("/system/promote/log")
async def promote_log(limit: int = 200, app: App = Depends(get_app)) -> list[dict[str, Any]]:
    return promote_mod.read_log(app, limit)


@router.get("/system/snapshots")
async def snapshots(app: App = Depends(get_app)) -> list[dict[str, Any]]:
    """Pre-promotion snapshots, newest first (03 §4.1)."""
    root = app.snapshots_dir
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for folder in sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True):
        state = folder / "state.db"
        out.append({
            "id": folder.name,
            "path": str(folder).replace("\\", "/"),
            "bytes": state.stat().st_size if state.exists() else 0,
            "has_state": state.exists(),
        })
    return out


def _client(app: App) -> Any:
    return app.supervisor or promote_mod.SupervisorClient(app)


def _supervisor_status(answer: dict[str, Any]) -> int:
    return 503 if answer.get("error") == promote_mod.NOT_RUNNING else 502
