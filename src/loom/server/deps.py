"""Dependencies every router uses. The App lives on `request.app.state.loom`; there is
no global, so tests can run several apps in one process."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, WebSocket

from ..app import App


def get_app(request: Request) -> App:
    loom: App | None = getattr(request.app.state, "loom", None)
    if loom is None:  # pragma: no cover - only if create_app was bypassed
        raise HTTPException(status_code=500, detail="app not initialised")
    return loom


def app_of(scope_app: Any) -> App:
    return scope_app.state.loom


def ws_app(websocket: WebSocket) -> App:
    return websocket.app.state.loom


def token_of(app: App) -> str:
    return str(app.settings.get("general.lan_token") or "")


def row(record: Any, json_columns: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """A table row as JSON (03 §21.1): JSON columns decoded, everything else as stored."""
    from .. import db

    out = db.row_to_dict(record)
    if out is None:
        return None
    for column in json_columns:
        if column in out:
            out[column] = db.loads(out[column], {} if column != "args" else {})
    return out
