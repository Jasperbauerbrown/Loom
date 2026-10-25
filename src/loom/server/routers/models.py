"""Providers, presets, roles and the live model list (03 §5.2, §12.1, §13.2 Models).

Three layers, three endpoint families, one event: every mutation emits
`models.changed {kind: provider|preset|role, name}` before it returns, so the Settings
screen and any other surface see the same rows without polling.

Deletes that would leave a dangling reference are refused with 409 and the names of the
referents — the UI shows the reason rather than discovering a broken role later.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ... import db
from ...app import App
from ...models import client as model_client
from ...models import roles as model_roles
from ...models.types import ModelError
from ..deps import get_app

router = APIRouter(tags=["models"])

PROVIDER_KINDS = ("ollama", "openai_compatible")


# ---------------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------------
class ProviderIn(BaseModel):
    name: str = Field(min_length=1)
    kind: str
    base_url: str = Field(min_length=1)
    api_key_ref: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ProviderPatch(BaseModel):
    kind: str | None = None
    base_url: str | None = None
    api_key_ref: str | None = None
    params: dict[str, Any] | None = None
    enabled: bool | None = None


class PresetIn(BaseModel):
    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""
    enabled: bool = True


class PresetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1)  # a different name renames (cascades)
    provider: str | None = None
    model: str | None = None
    params: dict[str, Any] | None = None
    notes: str | None = None
    enabled: bool | None = None


class RoleIn(BaseModel):
    preset: str | None = None


# ---------------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------------
@router.get("/providers")
async def list_providers(app: App = Depends(get_app)) -> list[dict[str, Any]]:
    return [_provider_json(app, r) for r in app.db.execute("SELECT * FROM providers ORDER BY name")]


@router.get("/providers/{name}")
async def read_provider(name: str, app: App = Depends(get_app)) -> dict[str, Any]:
    return _provider_json(app, _require(app, "providers", "name", name, "provider"))


@router.post("/providers", status_code=201)
async def create_provider(body: ProviderIn, app: App = Depends(get_app)) -> dict[str, Any]:
    _check_kind(body.kind)
    if app.db.execute("SELECT 1 FROM providers WHERE name=?", (body.name,)).fetchone():
        raise HTTPException(status_code=409, detail=f"provider {body.name!r} already exists")
    now = db.now_iso()
    app.db.execute(
        "INSERT INTO providers(name, kind, base_url, api_key_ref, params, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (body.name, body.kind, body.base_url.rstrip("/"), body.api_key_ref, db.dumps(body.params), int(body.enabled), now, now),
    )
    app.bus.emit("models.changed", {"kind": "provider", "name": body.name})
    return _provider_json(app, _require(app, "providers", "name", body.name, "provider"))


@router.patch("/providers/{name}")
async def update_provider(name: str, body: ProviderPatch, app: App = Depends(get_app)) -> dict[str, Any]:
    _require(app, "providers", "name", name, "provider")
    if body.kind is not None:
        _check_kind(body.kind)
    fields, values = _patch_fields(
        {
            "kind": body.kind,
            "base_url": body.base_url.rstrip("/") if body.base_url else None,
            "api_key_ref": body.api_key_ref,
            "params": db.dumps(body.params) if body.params is not None else None,
            "enabled": int(body.enabled) if body.enabled is not None else None,
        }
    )
    if fields:
        app.db.execute(
            f"UPDATE providers SET {', '.join(fields)}, updated_at=? WHERE name=?", (*values, db.now_iso(), name)
        )
        if body.params is not None and "concurrency" in body.params:
            _queue(app).set_concurrency(name, int(body.params["concurrency"]))
    app.bus.emit("models.changed", {"kind": "provider", "name": name})
    return _provider_json(app, _require(app, "providers", "name", name, "provider"))


@router.delete("/providers/{name}")
async def delete_provider(name: str, app: App = Depends(get_app)) -> dict[str, Any]:
    _require(app, "providers", "name", name, "provider")
    used = [r["name"] for r in app.db.execute("SELECT name FROM presets WHERE provider=?", (name,))]
    if used:
        raise HTTPException(status_code=409, detail=f"provider {name!r} is used by presets: {', '.join(used)}")
    app.db.execute("DELETE FROM providers WHERE name=?", (name,))
    app.bus.emit("models.changed", {"kind": "provider", "name": name})
    return {"ok": True, "deleted": name}


@router.post("/providers/{name}/probe")
async def probe_provider(name: str, role: str | None = None, app: App = Depends(get_app)) -> dict[str, Any]:
    """`?role=transcription` lists the provider's transcription models where it can say
    (OpenRouter's `output_modalities` filter), the same switch as `GET /models`."""
    _require(app, "providers", "name", name, "provider")
    try:
        models = await app.models.list_models(name, role=role)
    except ModelError as exc:
        return {"ok": False, "models": [], "error": exc.message, "outcome": exc.outcome}
    return {"ok": True, "models": models, "error": "", "outcome": "ok"}


# ---------------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------------
@router.get("/presets")
async def list_presets(app: App = Depends(get_app)) -> list[dict[str, Any]]:
    return [_preset_json(app, r) for r in app.db.execute("SELECT * FROM presets ORDER BY name")]


@router.get("/presets/{name}")
async def read_preset(name: str, app: App = Depends(get_app)) -> dict[str, Any]:
    return _preset_json(app, _require(app, "presets", "name", name, "preset"))


@router.post("/presets", status_code=201)
async def create_preset(body: PresetIn, app: App = Depends(get_app)) -> dict[str, Any]:
    if app.db.execute("SELECT 1 FROM presets WHERE name=?", (body.name,)).fetchone():
        raise HTTPException(status_code=409, detail=f"preset {body.name!r} already exists")
    if app.db.execute("SELECT 1 FROM providers WHERE name=?", (body.provider,)).fetchone() is None:
        raise HTTPException(status_code=400, detail=f"unknown provider {body.provider!r}")
    now = db.now_iso()
    app.db.execute(
        "INSERT INTO presets(name, provider, model, params, notes, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (body.name, body.provider, body.model, db.dumps(body.params), body.notes, int(body.enabled), now, now),
    )
    app.bus.emit("models.changed", {"kind": "preset", "name": body.name})
    return _preset_json(app, _require(app, "presets", "name", body.name, "preset"))


@router.patch("/presets/{name}")
async def update_preset(name: str, body: PresetPatch, app: App = Depends(get_app)) -> dict[str, Any]:
    current = _require(app, "presets", "name", name, "preset")
    if body.provider is not None and app.db.execute("SELECT 1 FROM providers WHERE name=?", (body.provider,)).fetchone() is None:
        raise HTTPException(status_code=400, detail=f"unknown provider {body.provider!r}")
    params = None
    if body.params is not None:
        # token_factor_seed is machine-written (03 §6.6); a UI edit never clears it.
        merged = dict(body.params)
        existing = db.loads(current["params"], {}) or {}
        if "token_factor_seed" not in merged and "token_factor_seed" in existing:
            merged["token_factor_seed"] = existing["token_factor_seed"]
        params = db.dumps(merged)
    new_name = (body.name or "").strip() or name
    if new_name != name and app.db.execute("SELECT 1 FROM presets WHERE name=?", (new_name,)).fetchone():
        raise HTTPException(status_code=409, detail=f"preset {new_name!r} already exists")
    fields, values = _patch_fields(
        {
            "name": new_name if new_name != name else None,
            "provider": body.provider,
            "model": body.model,
            "params": params,
            "notes": body.notes,
            "enabled": int(body.enabled) if body.enabled is not None else None,
        }
    )
    if fields:
        # A rename moves every reference with it in one unit. `model_roles.preset` is a
        # foreign key on the name, so the check is deferred to the commit; conversations
        # (live and archived) carry the name as plain text and are repointed the same way.
        with db.transaction(app.db):
            app.db.execute("PRAGMA defer_foreign_keys=ON")
            app.db.execute(
                f"UPDATE presets SET {', '.join(fields)}, updated_at=? WHERE name=?", (*values, db.now_iso(), name)
            )
            if new_name != name:
                app.db.execute("UPDATE model_roles SET preset=? WHERE preset=?", (new_name, name))
                app.db.execute("UPDATE conversations SET preset=? WHERE preset=?", (new_name, name))
    event: dict[str, Any] = {"kind": "preset", "name": new_name}
    if new_name != name:
        event["renamed_from"] = name
    app.bus.emit("models.changed", event)
    return _preset_json(app, _require(app, "presets", "name", new_name, "preset"))


@router.delete("/presets/{name}")
async def delete_preset(name: str, app: App = Depends(get_app)) -> dict[str, Any]:
    _require(app, "presets", "name", name, "preset")
    usage = _preset_usage(app, name)
    if usage["roles"] or usage["conversations"]:
        parts = []
        if usage["roles"]:
            parts.append("roles " + ", ".join(usage["roles"]))
        if usage["conversations"]:
            parts.append(f"{usage['conversations']} conversation(s)")
        raise HTTPException(status_code=409, detail=f"preset {name!r} is in use by " + " and ".join(parts))
    app.db.execute("DELETE FROM presets WHERE name=?", (name,))
    app.bus.emit("models.changed", {"kind": "preset", "name": name})
    return {"ok": True, "deleted": name}


@router.post("/presets/{name}/test")
async def test_preset(name: str, app: App = Depends(get_app)) -> dict[str, Any]:
    _require(app, "presets", "name", name, "preset")
    return await model_client.test_preset(app, name)


# ---------------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------------
@router.get("/model-roles")
async def list_roles(app: App = Depends(get_app)) -> list[dict[str, Any]]:
    rows = {r["role"]: r["preset"] for r in app.db.execute("SELECT role, preset FROM model_roles")}
    return [{"role": role, "preset": rows.get(role)} for role in model_roles.ROLES]


@router.put("/model-roles/{role}")
async def set_role(role: str, body: RoleIn, app: App = Depends(get_app)) -> dict[str, Any]:
    if role not in model_roles.ROLES:
        raise HTTPException(status_code=404, detail=f"unknown role {role!r}")
    preset = body.preset or None
    if preset is not None:
        row = app.db.execute("SELECT enabled FROM presets WHERE name=?", (preset,)).fetchone()
        if row is None:
            raise HTTPException(status_code=400, detail=f"unknown preset {preset!r}")
        if not row["enabled"]:
            raise HTTPException(status_code=400, detail=f"preset {preset!r} is disabled")
    app.db.execute("INSERT INTO model_roles(role, preset) VALUES (?, ?) "
                   "ON CONFLICT(role) DO UPDATE SET preset=excluded.preset", (role, preset))
    app.bus.emit("models.changed", {"kind": "role", "name": role})
    if app.has_model(role):
        # Same predicate that posted it: once it no longer holds, the role's setup item
        # ("Choose a model" for the desk under source `setup`, "Choose a transcription
        # model" under `setup:transcription`, 03 §5.7) is answered.
        from ... import items

        for item in items.by_source(app, "setup" if role == "desk" else f"setup:{role}"):
            if item["dismissed_at"] is None:
                items.dismiss(app, item["id"])
    return {"role": role, "preset": preset}


# ---------------------------------------------------------------------------------
# live model list
# ---------------------------------------------------------------------------------
@router.get("/models")
async def models(provider: str | None = None, role: str | None = None, app: App = Depends(get_app)) -> dict[str, Any]:
    """`role=transcription` asks providers that can filter (OpenRouter) for their
    speech-to-text models only (03 §5.7); other roles list everything."""
    names = [provider] if provider else [r["name"] for r in app.db.execute("SELECT name FROM providers WHERE enabled=1 ORDER BY name")]
    out: list[dict[str, Any]] = []
    for name in names:
        if app.db.execute("SELECT 1 FROM providers WHERE name=?", (name,)).fetchone() is None:
            raise HTTPException(status_code=404, detail=f"unknown provider {name!r}")
        try:
            out.append({"provider": name, "models": await app.models.list_models(name, role=role), "error": None})
        except ModelError as exc:
            out.append({"provider": name, "models": [], "error": exc.message})
    return {"providers": out}


# ---------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------
def _queue(app: App):  # type: ignore[no-untyped-def]
    return getattr(app.models, "queue", None) or _Noop()


class _Noop:
    def set_concurrency(self, *_: Any) -> None:
        pass


def _check_kind(kind: str) -> None:
    if kind not in PROVIDER_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {list(PROVIDER_KINDS)}")


def _require(app: App, table: str, column: str, value: str, label: str) -> Any:
    row = app.db.execute(f"SELECT * FROM {table} WHERE {column}=?", (value,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown {label} {value!r}")
    return row


def _patch_fields(values: dict[str, Any]) -> tuple[list[str], list[Any]]:
    fields, out = [], []
    for key, value in values.items():
        if value is not None:
            fields.append(f"{key}=?")
            out.append(value)
    return fields, out


def _provider_json(app: App, row: Any) -> dict[str, Any]:
    out = db.row_to_dict(row) or {}
    out["params"] = db.loads(out.get("params"), {}) or {}
    out["enabled"] = bool(out.get("enabled"))
    out["effective_params"] = model_roles.provider_params({**out, "params": out["params"]})
    out["presets"] = [r["name"] for r in app.db.execute("SELECT name FROM presets WHERE provider=?", (out["name"],))]
    return out


def _preset_json(app: App, row: Any) -> dict[str, Any]:
    out = db.row_to_dict(row) or {}
    params = db.loads(out.get("params"), {}) or {}
    out["params"] = params
    out["enabled"] = bool(out.get("enabled"))
    out["token_factor_seed"] = params.get("token_factor_seed")  # read-only, machine-written
    out["usage"] = _preset_usage(app, str(out["name"]))
    return out


def _preset_usage(app: App, name: str) -> dict[str, Any]:
    roles_using = [r["role"] for r in app.db.execute("SELECT role FROM model_roles WHERE preset=?", (name,))]
    conversations = app.db.execute(
        "SELECT COUNT(*) FROM conversations WHERE preset=? AND status NOT IN ('archived','done','failed')", (name,)
    ).fetchone()[0]
    return {"roles": roles_using, "conversations": int(conversations)}
