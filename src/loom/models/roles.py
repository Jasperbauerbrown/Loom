"""Role → preset → provider resolution (03 §5.2). The one place that answers
"which model, with which parameters" for a call.

    resolved = roles.resolve(app, role="desk")            # or preset="qwen36-local"
    request  = roles.build_request(app, role="desk", messages=[...], tools=[...])

Layering, key-wise: provider.params < preset.params < per-call overrides. A preset
override always wins over a role. `utility` falls back to `desk` when unset. A missing
role preset is a `ModelError("fatal", ...)` naming the settings URL — never a silent
default, because guessing a model is worse than saying so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import db
from .types import DEFAULT_IMAGE_TOKENS, ChatRequest, ModelError, image_tokens, is_multimodal  # noqa: F401  (re-exported)

ROLES = ("desk", "worker", "maintenance", "utility", "embedding")

# Params the merge understands. Anything else a user puts on a provider or preset row is
# carried through untouched: the transport decides what it can use.
PRESET_PARAM_KEYS = (
    "max_tokens",
    "temperature",
    "top_p",
    "effective_ctx",
    "num_ctx",
    "keep_alive",
    "timeout_s",
    "reasoning",
    "thinking_cap",  # Loom-side: enforced by the runner on the stream, never sent
    "token_factor_seed",
    "multimodal",
    "image_tokens",
    "repetition_penalty",
    "presence_penalty",
    "frequency_penalty",
)
PROVIDER_PARAM_KEYS = ("timeout_s", "concurrency", "stream_idle_s")

DEFAULT_CONCURRENCY = {"ollama": 1, "openai_compatible": 4}
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_STREAM_IDLE_S = 120.0


@dataclass
class Resolved:
    provider: dict[str, Any]
    preset: dict[str, Any]
    params: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    role: str = ""

    @property
    def provider_name(self) -> str:
        return str(self.provider["name"])

    @property
    def kind(self) -> str:
        return str(self.provider["kind"])


def settings_url(app) -> str:  # type: ignore[no-untyped-def]
    host = app.settings.get("general.bind_host") or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{app.settings.get('general.port')}/settings/models"


def _row(app, table: str, key_column: str, key: str) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    row = app.db.execute(f"SELECT * FROM {table} WHERE {key_column}=?", (key,)).fetchone()
    out = db.row_to_dict(row)
    if out is not None:
        out["params"] = db.loads(out.get("params"), {}) or {}
    return out


def preset_row(app, name: str) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    return _row(app, "presets", "name", name)


def provider_row(app, name: str) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    return _row(app, "providers", "name", name)


def role_preset(app, role: str) -> str | None:  # type: ignore[no-untyped-def]
    row = app.db.execute("SELECT preset FROM model_roles WHERE role=?", (role,)).fetchone()
    return (row["preset"] or None) if row else None


def fallback(app) -> str | None:  # type: ignore[no-untyped-def]
    """`models.fallback_role`, or None when unset. The runner decides what to do with it
    (03 §5.5); resolution itself never falls back silently."""
    value = app.settings.get("models.fallback_role") or ""
    return value or None


def provider_params(provider: dict[str, Any]) -> dict[str, Any]:
    params = dict(provider.get("params") or {})
    params.setdefault("concurrency", DEFAULT_CONCURRENCY.get(str(provider.get("kind")), 1))
    params.setdefault("timeout_s", DEFAULT_TIMEOUT_S)
    params.setdefault("stream_idle_s", DEFAULT_STREAM_IDLE_S)
    return params


def resolve(app, role: str | None = None, preset: str | None = None) -> Resolved:  # type: ignore[no-untyped-def]
    """Resolve a call's model. `preset` (an explicit override) wins over `role`."""
    name = preset or None
    effective_role = role or ""
    if name is None:
        if not effective_role:
            raise ModelError("fatal", "resolve() needs a role or a preset")
        if effective_role not in ROLES:
            raise ModelError("fatal", f"unknown model role {effective_role!r}")
        name = role_preset(app, effective_role)
        if name is None and effective_role == "utility":
            effective_role = "desk"  # 03 §5.2: utility falls back to desk
            name = role_preset(app, "desk")
        if name is None:
            raise ModelError(
                "fatal",
                f"no model configured for role {role} — open {settings_url(app)}",
            )
    preset_data = preset_row(app, name)
    if preset_data is None:
        raise ModelError("fatal", f"unknown preset {name!r} — open {settings_url(app)}")
    if not preset_data.get("enabled"):
        raise ModelError("fatal", f"preset {name!r} is disabled — open {settings_url(app)}")
    provider_data = provider_row(app, str(preset_data["provider"]))
    if provider_data is None:
        raise ModelError("fatal", f"preset {name!r} points at unknown provider {preset_data['provider']!r}")
    if not provider_data.get("enabled"):
        raise ModelError("fatal", f"provider {provider_data['name']!r} is disabled — open {settings_url(app)}")

    params = provider_params(provider_data)
    params.update({k: v for k, v in (preset_data.get("params") or {}).items() if v is not None})
    return Resolved(
        provider=provider_data,
        preset=preset_data,
        params=params,
        model=str(preset_data["model"]),
        role=effective_role,
    )


def build_request(
    app,  # type: ignore[no-untyped-def]
    *,
    role: str | None = None,
    preset: str | None = None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    overrides: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    purpose: str = "turn",
) -> ChatRequest:
    """The only way a ChatRequest is constructed outside tests."""
    resolved = resolve(app, role=role, preset=preset)
    params = dict(resolved.params)
    for key, value in (overrides or {}).items():
        if value is not None:
            params[key] = value
    return ChatRequest(
        messages=messages,
        tools=tools,
        params=params,
        preset=str(resolved.preset["name"]),
        provider=resolved.provider_name,
        model=resolved.model,
        conversation_id=conversation_id,
        purpose=purpose,
    )


def update_token_factor_seed(app, preset_name: str, factor: float) -> None:  # type: ignore[no-untyped-def]
    """Calibration writes the factor back to its preset (03 §6.6: the factor is a
    property of model + content, so the preset is its home)."""
    row = preset_row(app, preset_name)
    if row is None:
        return
    params = dict(row.get("params") or {})
    params["token_factor_seed"] = round(float(factor), 4)
    app.db.execute(
        "UPDATE presets SET params=?, updated_at=? WHERE name=?",
        (db.dumps(params), db.now_iso(), preset_name),
    )
    app.bus.emit("models.changed", {"kind": "preset", "name": preset_name})


def token_factor_seed(app, preset_name: str, default: float = 1.0) -> float:  # type: ignore[no-untyped-def]
    row = preset_row(app, preset_name)
    if row is None:
        return default
    try:
        return float((row.get("params") or {}).get("token_factor_seed") or default)
    except (TypeError, ValueError):
        return default
