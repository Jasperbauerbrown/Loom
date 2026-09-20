"""Schedules (03 §12.1, §13.2 Triggers). The same `Scheduler` the tools use, so the UI
and the model can never hold different schedules.

Errors: 400 for a spec that does not parse, 404 for an unknown id, 409 for a refusal
(caps, unknown target, bad policy). Every mutation emits its `schedule.*` WS event
before the response returns.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ...app import App
from ...triggers.scheduler import SchedulerError
from ...triggers.spec import SpecError
from ..deps import get_app

router = APIRouter(tags=["schedules"])

FIELDS = ("spec", "target", "payload", "policy", "enabled")


def _scheduler(app: App):  # type: ignore[no-untyped-def]
    if app.scheduler is None:  # pragma: no cover - triggers.install always attaches one
        raise HTTPException(status_code=503, detail="scheduler not available")
    return app.scheduler


def _row(app: App, schedule_id: str) -> dict[str, Any]:
    row = _scheduler(app).get(schedule_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
    return row


@router.get("/schedules")
async def list_schedules(include_disabled: bool = True, app: App = Depends(get_app)) -> list[dict[str, Any]]:
    return _scheduler(app).list(include_disabled=include_disabled)


@router.post("/schedules", status_code=201)
async def create_schedule(body: dict[str, Any] = Body(...), app: App = Depends(get_app)) -> dict[str, Any]:
    body = body or {}
    try:
        return _scheduler(app).create(
            spec=str(body.get("spec") or ""),
            target=str(body.get("target") or "desk"),
            payload=str(body.get("payload") or ""),
            policy=body.get("policy"),
            created_by=str(body.get("created_by") or "user"),
        )
    except SpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SchedulerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/schedules/{schedule_id}")
async def get_schedule(schedule_id: str, app: App = Depends(get_app)) -> dict[str, Any]:
    return _row(app, schedule_id)


@router.patch("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: str, body: dict[str, Any] = Body(...), app: App = Depends(get_app)
) -> dict[str, Any]:
    _row(app, schedule_id)
    fields = {k: v for k, v in (body or {}).items() if k in FIELDS}
    if not fields:
        raise HTTPException(status_code=400, detail=f"nothing to update; fields are {list(FIELDS)}")
    try:
        return _scheduler(app).update(schedule_id, **fields)
    except SpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SchedulerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str, app: App = Depends(get_app)) -> dict[str, Any]:
    _row(app, schedule_id)
    return _scheduler(app).delete(schedule_id)


@router.post("/schedules/{schedule_id}/run")
async def run_schedule(schedule_id: str, app: App = Depends(get_app)) -> dict[str, Any]:
    _row(app, schedule_id)
    try:
        return _scheduler(app).run_now(schedule_id)
    except SchedulerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
