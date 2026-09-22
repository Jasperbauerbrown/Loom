"""System endpoints (03 §12.1): health, version, queue, logs, reindex, open a folder."""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ... import db
from ...app import App
from ..deps import get_app

log = logging.getLogger(__name__)
router = APIRouter(tags=["system"])


@router.get("/system/health")
async def health(app: App = Depends(get_app)) -> dict[str, Any]:
    hub = app.extras.get("hub")
    return {
        "ok": not app.shutting_down,
        "version": app.version,
        "uptime_s": uptime_s(app),
        "first_run": app.first_run,
        "model_configured": app.has_model("desk"),
        "ws_clients": hub.connected() if hub else 0,
    }


@router.get("/system/version")
async def version(app: App = Depends(get_app)) -> dict[str, Any]:
    release = None
    pointer = app.home / "current"
    if pointer.exists():
        release = pointer.read_text(encoding="utf-8").strip() or None
    return {
        "version": app.version,
        "release": release,
        "schema_version": db.current_version(app.db),
        "home": str(app.home),
    }


@router.get("/system/queue")
async def queue(app: App = Depends(get_app)) -> dict[str, Any]:
    return app.models.queue_state()


@router.get("/system/logs")
async def logs(
    since: str | None = None,
    level: str | None = None,
    limit: int = 500,
    app: App = Depends(get_app),
) -> dict[str, Any]:
    from ... import log as loom_log

    return {"records": loom_log.recent(since, level, max(1, min(int(limit), 2000)))}


class OpenBody(BaseModel):
    path: str


def _open_in_file_manager(target: str) -> None:
    """Show a folder in the OS file manager. Windows: Explorer; macOS: Finder; else xdg-open."""
    import os
    import subprocess
    import sys

    if os.name == "nt":
        os.startfile(target)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])


@router.post("/system/open")
async def open_path(body: OpenBody, app: App = Depends(get_app)) -> dict[str, Any]:
    """Open a folder (or a file's folder) in the file manager. Only paths under APP_HOME
    or the allowed roots: the same boundary the assistant's own file tools have."""
    import asyncio
    import os

    from ...tools import fs_guard

    raw = body.path.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="path is empty")
    if not os.path.isabs(raw) and not raw.startswith(("~", "/")):
        raw = str(app.vault_dir / raw)  # vault-relative, as the Vault screen sends it
    path = fs_guard.normalise(raw)
    allowed = [fs_guard.normalise(str(app.home))] + [fs_guard.normalise(r) for r in fs_guard.effective_roots(app, app.conversation(app.desk_id()))]
    if not any(path == root or path.startswith(root.rstrip("/") + "/") for root in allowed):
        raise HTTPException(status_code=403, detail="only folders under Loom's home or the allowed roots can be opened")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"{path} does not exist")
    target = path if os.path.isdir(path) else os.path.dirname(path)
    await asyncio.to_thread(_open_in_file_manager, target)
    return {"opened": target}


@router.post("/system/reindex")
async def reindex(app: App = Depends(get_app)) -> dict[str, Any]:
    if app.index is None:
        raise HTTPException(status_code=503, detail="the knowledge index is not installed")
    result = await app.index.a_rebuild()  # rebuild() is sync; a_rebuild adds embeddings
    app.bus.emit("knowledge.changed", {"paths": []})
    return result if isinstance(result, dict) else {"ok": True}


# ---------------------------------------------------------------------------------
def uptime_s(app: App) -> int:
    try:
        started = _dt.datetime.fromisoformat(app.started_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0
    return max(0, int((_dt.datetime.now(_dt.timezone.utc) - started).total_seconds()))
