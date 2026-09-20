"""Approval endpoints (03 §12.1). The user's half of the permission loop: see what is
waiting, answer it. Every answer emits `approval.resolved` and, for a real tool call,
wakes the conversation so the turn resumes where it stopped.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ...app import App
from ...tools import approvals
from ..deps import get_app

router = APIRouter(tags=["approvals"])


@router.get("/approvals")
async def list_approvals(
    status: str | None = None, conversation_id: str | None = None, app: App = Depends(get_app)
) -> list[dict[str, Any]]:
    return approvals.listed(app, status=status, conversation_id=conversation_id)


@router.get("/approvals/{approval_id}")
async def read_approval(approval_id: str, app: App = Depends(get_app)) -> dict[str, Any]:
    row = approvals.get(app, approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no approval {approval_id}")
    return row


@router.post("/approvals/{approval_id}")
async def decide_approval(
    approval_id: str, body: dict[str, Any] = Body(...), app: App = Depends(get_app)
) -> dict[str, Any]:
    decision = str((body or {}).get("decision") or "")
    add_root = bool((body or {}).get("add_root") or False)
    if decision not in approvals.DECISIONS:
        raise HTTPException(status_code=400, detail=f"decision must be one of {list(approvals.DECISIONS)}")
    try:
        return approvals.decide(app, approval_id, decision, add_root=add_root)
    except approvals.NotFound:
        raise HTTPException(status_code=404, detail=f"no approval {approval_id}") from None
    except approvals.AlreadyDecided as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
