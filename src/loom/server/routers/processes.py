"""Background processes (03 §9.6): list, inspect, read logs, stop. Starting one is a
model action (`shell(background=true)`); the person manages them from here and the UI."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_app

router = APIRouter()


@router.get("/processes")
async def list_processes(conversation_id: str | None = None, status: str | None = None, app=Depends(get_app)) -> dict[str, Any]:
    return {"processes": app.processes.list(conversation_id=conversation_id, status=status)}


@router.get("/processes/{process_id}")
async def get_process(process_id: str, app=Depends(get_app)) -> dict[str, Any]:
    row = app.processes.get(process_id)
    if row is None:
        raise HTTPException(404, f"process {process_id} not found")
    return row


@router.get("/processes/{process_id}/logs")
async def process_logs(process_id: str, offset: int | None = None, limit: int | None = None, app=Depends(get_app)) -> dict[str, Any]:
    page = app.processes.read_log(process_id, offset, limit)
    if page is None:
        raise HTTPException(404, f"process {process_id} not found")
    return page


@router.post("/processes/{process_id}/stop")
async def stop_process(process_id: str, app=Depends(get_app)) -> dict[str, Any]:
    row = await app.processes.stop(process_id, actor="user")
    if row is None:
        raise HTTPException(404, f"process {process_id} not found")
    return row
