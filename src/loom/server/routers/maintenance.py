"""Maintenance status and the "run maintenance now" action (03 §12.1, §13.1).

`GET /maintenance` is the whole visible surface of the role: whether a pass is running,
when the nightly one is next due, how much of the idle window is left, and the full
report of the last pass. `POST /maintenance/run` starts one out of band; it returns
`202` immediately because a pass takes model calls, and `409` when one is already in
flight (passes never queue — the next pass would see the same world).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...app import App
from ..deps import get_app

router = APIRouter(tags=["maintenance"])


def _maintenance(app: App) -> Any:
    if app.maintenance is None:  # pragma: no cover - only if the package was not installed
        raise HTTPException(503, "maintenance is not installed")
    return app.maintenance


@router.get("/maintenance")
async def status(app: App = Depends(get_app)) -> dict[str, Any]:
    return _maintenance(app).status()


@router.post("/maintenance/run", status_code=202)
async def run(app: App = Depends(get_app)) -> dict[str, Any]:
    maintenance = _maintenance(app)
    if maintenance.lock.locked():
        raise HTTPException(409, "a maintenance pass is already running")
    app.spawn("maintenance-run", maintenance.run("manual"))
    return {"started": True, **maintenance.status()}
