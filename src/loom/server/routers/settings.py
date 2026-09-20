"""Settings endpoints (03 §4.6, §21.1). Values *and* schema, so the UI renders every
setting from the schema and no knob can exist without a UI (charter §2.2)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ...app import App
from ...config import Settings
from ..deps import get_app

router = APIRouter(tags=["settings"])


@router.get("/settings")
async def read_settings(app: App = Depends(get_app)) -> dict[str, Any]:
    return {"values": app.settings.to_json(), "schema": Settings.schema(), "groups": Settings.groups()}


@router.patch("/settings")
async def patch_settings(patch: dict[str, Any] = Body(...), app: App = Depends(get_app)) -> dict[str, Any]:
    if not isinstance(patch, dict) or not patch:
        raise HTTPException(status_code=400, detail="expected a non-empty {key: value} object")
    try:
        changed = app.settings.update(app.db, patch)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"unknown setting {exc.args[0]!r}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    app.bus.emit("settings.changed", {"keys": changed})
    return {"values": app.settings.to_json(), "changed": changed}
