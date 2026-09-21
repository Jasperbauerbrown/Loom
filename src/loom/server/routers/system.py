"""System endpoints (03 §12.1): health, version, queue, logs, reindex, first-run setup."""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ... import db, items
from ...app import App
from ...config import detect_timezone
from ...models import client as model_client
from ...models.types import ModelError
from ..deps import get_app

log = logging.getLogger(__name__)
router = APIRouter(tags=["system"])

EMBEDDING_HINTS = ("embed", "bge", "gte", "minilm")

# Sensible preset defaults for the one required choice on the Setup screen (03 §3.3).
SETUP_PRESET_PARAMS: dict[str, Any] = {
    "max_tokens": 4096,
    "temperature": 0.3,
    "effective_ctx": 32768,
    "num_ctx": 32768,
    "keep_alive": "30m",
    "reasoning": "on",
}


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
# first run (03 §3.3)
# ---------------------------------------------------------------------------------
class OpenRouter(BaseModel):
    base_url: str = "https://openrouter.ai/api"
    api_key: str


class SetupRequest(BaseModel):
    model: str = Field(min_length=1)
    provider: str | None = None
    embedding_model: str | None = None
    timezone: str | None = None
    projects_root: str | None = None
    openrouter: OpenRouter | None = None


@router.get("/system/setup")
async def setup_state(app: App = Depends(get_app)) -> dict[str, Any]:
    detected = []
    embedding_available = False
    for row in app.db.execute("SELECT name, kind, base_url FROM providers WHERE enabled=1 ORDER BY name"):
        entry = {"name": row["name"], "kind": row["kind"], "base_url": row["base_url"], "models": [], "error": None}
        try:
            entry["models"] = await app.models.list_models(row["name"])
        except ModelError as exc:
            entry["error"] = exc.message
        except Exception as exc:  # a probe must never break the Setup screen
            entry["error"] = str(exc)
        if any(_looks_like_embedding(m) for m in entry["models"]):
            embedding_available = True
        detected.append(entry)
    return {
        "needed": not app.has_model("desk"),
        "providers_detected": detected,
        "timezone": app.settings.get("general.timezone") or detect_timezone(),
        "projects_root": str(app.settings.projects_root()),
        "embedding_available": embedding_available or bool(_role_preset(app, "embedding")),
    }


@router.post("/system/setup")
async def run_setup(body: SetupRequest, app: App = Depends(get_app)) -> dict[str, Any]:
    now = db.now_iso()
    provider_name = body.provider or ""

    if body.openrouter is not None:
        if app.secrets is None:
            raise HTTPException(
                status_code=503,
                detail="the secrets store is unavailable, so an OpenRouter key cannot be saved",
            )
        try:
            app.secrets.set("openrouter_api_key", body.openrouter.api_key)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"storing the OpenRouter key failed: {exc}") from exc
        provider_name = provider_name or "openrouter"
        _upsert_provider(
            app,
            provider_name,
            kind="openai_compatible",
            base_url=body.openrouter.base_url.rstrip("/"),
            api_key_ref="openrouter_api_key",
            now=now,
        )
    if not provider_name:
        row = app.db.execute("SELECT name FROM providers WHERE enabled=1 ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            raise HTTPException(status_code=400, detail="no provider configured or detected; add one first")
        provider_name = row["name"]
    elif app.db.execute("SELECT 1 FROM providers WHERE name=?", (provider_name,)).fetchone() is None:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider_name!r}")

    preset_name = _preset_name(body.model)
    with db.transaction(app.db):
        _write_preset(app, preset_name, provider_name, body.model, dict(SETUP_PRESET_PARAMS), now)
        for role in ("desk", "worker", "maintenance"):
            app.db.execute("UPDATE model_roles SET preset=? WHERE role=?", (preset_name, role))
        if body.embedding_model:
            embed_preset = _preset_name(body.embedding_model)
            _write_preset(app, embed_preset, provider_name, body.embedding_model, {}, now)
            app.db.execute("UPDATE model_roles SET preset=? WHERE role='embedding'", (embed_preset,))

    changed: list[str] = []
    try:
        patch: dict[str, Any] = {}
        if body.timezone:
            patch["general.timezone"] = body.timezone
        if body.projects_root:
            patch["general.projects_root"] = body.projects_root
        if body.embedding_model:
            patch["models.embeddings_enabled"] = True
        if patch:
            changed = app.settings.update(app.db, patch)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    items.post(
        app,
        "notification",
        "Welcome",
        "Tell me about yourself, or point me at something to learn.",
        source="setup",
    )
    if changed:
        app.bus.emit("settings.changed", {"keys": changed})
    app.bus.emit("models.changed", {"kind": "role", "name": "desk"})
    return {
        "ok": True,
        "preset": preset_name,
        "provider": provider_name,
        "roles": _roles(app),
        "values": app.settings.to_json(),
    }


# ---------------------------------------------------------------------------------
def uptime_s(app: App) -> int:
    try:
        started = _dt.datetime.fromisoformat(app.started_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0
    return max(0, int((_dt.datetime.now(_dt.timezone.utc) - started).total_seconds()))


def _looks_like_embedding(model: str) -> bool:
    lowered = model.lower()
    return any(hint in lowered for hint in EMBEDDING_HINTS)


def _role_preset(app: App, role: str) -> str | None:
    row = app.db.execute("SELECT preset FROM model_roles WHERE role=?", (role,)).fetchone()
    return (row["preset"] or None) if row else None


def _roles(app: App) -> dict[str, str | None]:
    return {r["role"]: r["preset"] for r in app.db.execute("SELECT role, preset FROM model_roles")}


def _preset_name(model: str) -> str:
    return model.replace(":latest", "").replace("/", "-").replace(":", "-")


def _upsert_provider(app: App, name: str, *, kind: str, base_url: str, api_key_ref: str | None, now: str) -> None:
    app.db.execute(
        "INSERT INTO providers(name, kind, base_url, api_key_ref, params, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, '{}', 1, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, base_url=excluded.base_url, "
        "api_key_ref=excluded.api_key_ref, enabled=1, updated_at=excluded.updated_at",
        (name, kind, base_url, api_key_ref, now, now),
    )
    app.bus.emit("models.changed", {"kind": "provider", "name": name})


def _write_preset(app: App, name: str, provider: str, model: str, params: dict[str, Any], now: str) -> None:
    app.db.execute(
        "INSERT INTO presets(name, provider, model, params, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET provider=excluded.provider, model=excluded.model, "
        "params=excluded.params, enabled=1, updated_at=excluded.updated_at",
        (name, provider, model, db.dumps(params), now, now),
    )
