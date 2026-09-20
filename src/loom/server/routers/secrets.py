"""Secret endpoints (03 §4.5, §13.2 Integrations). Names in, values write-only.

`GET` returns the names, the backend in use, and the warning that is non-empty when the
unencrypted file fallback is active — the Settings screen shows it as a persistent
banner, because a user who thinks their tokens are in the Credential Manager when they
are in a JSON file has been misled by us.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ...app import App
from ..deps import get_app

router = APIRouter(tags=["secrets"])


def _store(app: App) -> Any:
    if app.secrets is None:  # pragma: no cover - install always attaches it
        raise HTTPException(status_code=500, detail="no secret store")
    return app.secrets


@router.get("/secrets")
async def list_secrets(app: App = Depends(get_app)) -> dict[str, Any]:
    return _store(app).to_json()


@router.put("/secrets/{name}")
async def set_secret(name: str, body: dict[str, Any] = Body(...), app: App = Depends(get_app)) -> dict[str, Any]:
    value = (body or {}).get("value")
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=400, detail="value must be a non-empty string")
    store = _store(app)
    try:
        store.set(name, value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    app.bus.emit("settings.changed", {"keys": ["secrets"]})
    return {"name": name, "stored": True, "backend": store.backend, "warning": store.warning}


@router.delete("/secrets/{name}")
async def delete_secret(name: str, app: App = Depends(get_app)) -> dict[str, Any]:
    store = _store(app)
    if not store.delete(name):
        raise HTTPException(status_code=404, detail=f"no secret named {name!r}")
    app.bus.emit("settings.changed", {"keys": ["secrets"]})
    return {"name": name, "deleted": True}
