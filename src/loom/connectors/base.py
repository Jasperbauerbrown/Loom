"""The native-connector contract (03 §10.2, charter §5.8 tier 3).

A connector is one concept with up to four facets:

    tools    capabilities the model may call        (`<connector>_<tool>`)
    events   things that happen and want a turn     (→ the event router, 03 §11.2)
    surface  another way for the user to talk       (the adapter contract, 03 §12.3)
    hooks    a look at every tool call              (→ `loom.tools.hooks`, 03 §9.3 step 4½)

A package under `<repo>/connectors/<name>/` exports `CONNECTOR = Connector(...)`. The
contract is deliberately small: the assistant is expected to be able to write one for
itself (charter §5.9).

```python
from loom.connectors.base import Connector, Event

CONNECTOR = Connector(
    name="example_echo",
    description="Echoes text back.",
    config_schema={"type": "object", "properties": {...}, "required": []},
    tools=[Tool(...)],          # or a callable taking the config and returning tools
    events=SomeEventSource(),   # optional: .events(config) -> async iterator of Event
    surface=None,               # optional: .start(api, config) / .stop()
    setup=None,                 # optional: config -> a setup item's fields, or None
    before_tool=None,           # optional: see below
    after_tool=None,            # optional: see below
)


async def before_tool(api, config, ctx_summary, tool_name, args) -> dict | None:
    "Called once a call has been allowed and before it executes."
    return {"ask": True, "reason": "production database", "notes": ["read-only today"]}


async def after_tool(api, config, ctx_summary, tool_name, args, result) -> dict | None:
    "Called on every executed result, including one the runner ran after an approval."
    return {"notes": ["remember to update the runbook"]}
```

A hook **annotates or asks**; it can never deny (`{"decision": "deny"}` is logged and
counted as an ask at most) and it can never change what executes: `args` is a copy and
`ctx_summary` a plain dict built from the `ToolContext`, never the live object. A
before-hook sees `{{secret:NAME}}` placeholders in `args`, never a substituted secret —
substitution is step 5 of the dispatcher, after the hooks. Both may be `async def` or a
plain function; each call is bounded by `loom.tools.hooks.HOOK_TIMEOUT_S`, and one that
raises, times out or answers the wrong type is logged and ignored. `None` or `{}` means
nothing to say, and unknown keys are ignored.

`api` is the same `SurfaceApi` a surface gets: one is created per connector when it
loads and closed when it is torn down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Protocol, runtime_checkable

from ..tools.registry import Tool


class ConnectorError(Exception):
    """Bad configuration or a bad request about a connector: the router answers 400."""


@dataclass
class Event:
    """Something the connector saw. The router turns it into an inbox row and an item."""

    kind: str
    title: str
    content: str
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class EventSource(Protocol):
    def events(self, config: dict[str, Any]) -> AsyncIterator[Event]:
        """An async iterator of events. Run until cancelled; raising restarts it with
        backoff and marks the connector `error` (03 §17)."""
        ...


@runtime_checkable
class Surface(Protocol):
    async def start(self, api: "SurfaceApi", config: dict[str, Any]) -> None: ...

    async def stop(self) -> None: ...


#: `before_tool(api, config, ctx_summary, tool_name, args)` → `{"ask", "reason", "notes"}`
BeforeToolHook = Callable[..., Any]
#: `after_tool(api, config, ctx_summary, tool_name, args, result)` → `{"notes"}`
AfterToolHook = Callable[..., Any]


@dataclass
class Connector:
    name: str
    description: str = ""
    config_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    tools: list[Tool] | Callable[[dict[str, Any]], list[Tool]] = field(default_factory=list)
    events: EventSource | None = None
    surface: Surface | None = None
    setup: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
    before_tool: BeforeToolHook | None = None  # annotate or ask before a call executes
    after_tool: AfterToolHook | None = None  # annotate an executed result

    def tools_for(self, config: dict[str, Any]) -> list[Tool]:
        """Tools may depend on the config (one per configured account, say)."""
        value = self.tools(config) if callable(self.tools) else self.tools
        return list(value or [])

    @property
    def facets(self) -> list[str]:
        out = []
        if self.tools:
            out.append("tools")
        if self.events is not None:
            out.append("events")
        if self.surface is not None:
            out.append("surface")
        if self.before_tool is not None or self.after_tool is not None:
            out.append("hooks")
        return out


@dataclass
class ConnectorHooks:
    """One loaded connector's tool hooks, as `Loader.hooks()` hands them to
    `loom.tools.hooks`. Only loaded connectors have a record: disabling or reloading
    one takes its hooks with it."""

    name: str
    api: "SurfaceApi"
    config: dict[str, Any] = field(default_factory=dict)
    before: BeforeToolHook | None = None
    after: AfterToolHook | None = None


class SurfaceApi:
    """The in-process client a connector surface receives (03 §12.3).

    Thin wrappers over what the backend already has — `conversation.post`, the event
    bus, the approval decider, the conversations table — so a surface has exactly the
    web UI's powers and no private path of its own."""

    def __init__(self, app: Any, name: str):
        self.app = app
        self.name = name
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def send(self, conversation_id: str | None, text: str, user_id: str = "") -> int:
        """Deliver a user message from the surface; returns the inbox row id. A missing
        conversation id means the desk (03 §12.3)."""
        from ..conversation import post

        target = conversation_id or self.app.desk_id()
        return post(self.app, target, text, source=f"connector:{self.name}", meta={"user_id": user_id} if user_id else None)

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """Every bus event (03 §12.2). Returns an unsubscribe callable."""
        self.app.bus.listen(callback)
        self._listeners.append(callback)

        def unsubscribe() -> None:
            self.app.bus.unlisten(callback)
            if callback in self._listeners:
                self._listeners.remove(callback)

        return unsubscribe

    def answer_approval(self, approval_id: str, decision: str) -> dict[str, Any]:
        from ..tools import approvals

        return approvals.decide(self.app, approval_id, decision)

    def list_conversations(self) -> list[dict[str, Any]]:
        from .. import db

        return [db.row_to_dict(r) for r in self.app.db.execute("SELECT * FROM conversations ORDER BY updated_at DESC")]  # type: ignore[misc]

    def close(self) -> None:
        for callback in list(self._listeners):
            self.app.bus.unlisten(callback)
        self._listeners.clear()


# ---------------------------------------------------------------------------------
# config validation: the subset of JSON Schema the Settings form renders
# ---------------------------------------------------------------------------------
TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate_config(schema: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Check a connector config against its `config_schema` and fill in defaults.

    Supports `properties` with `type`, `enum` and `default`, plus `required`. Anything
    richer is not rendered by the Settings form either, so it would be a setting that
    exists without a UI (charter §2.2). Raises `ConnectorError` with one message."""
    if not isinstance(config, dict):
        raise ConnectorError("config must be an object")
    schema = schema or {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = [str(k) for k in (schema.get("required") or [])]
    out = dict(config)

    unknown = [k for k in out if k not in properties and k != "conversation_id"]
    if properties and unknown:
        raise ConnectorError(f"unknown setting(s): {', '.join(sorted(unknown))}")

    for key, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        if key not in out or out[key] is None:
            if "default" in spec:
                out[key] = spec["default"]
            continue
        value = out[key]
        kind = spec.get("type")
        if isinstance(kind, str) and kind in TYPES:
            if kind == "boolean" and not isinstance(value, bool):
                raise ConnectorError(f"{key} must be true or false")
            if kind != "boolean" and (isinstance(value, bool) or not isinstance(value, TYPES[kind])):
                raise ConnectorError(f"{key} must be a {kind}")
        choices = spec.get("enum")
        if isinstance(choices, list) and value not in choices:
            raise ConnectorError(f"{key} must be one of: {', '.join(str(c) for c in choices)}")

    missing = [k for k in required if out.get(k) in (None, "")]
    if missing:
        raise ConnectorError(f"missing required setting(s): {', '.join(missing)}")
    return out


def missing_required(schema: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Required keys with no value — the `needs_config` test, which must not raise."""
    required = [str(k) for k in ((schema or {}).get("required") or [])]
    return [k for k in required if (config or {}).get(k) in (None, "")]


__all__ = [
    "AfterToolHook",
    "BeforeToolHook",
    "Connector",
    "ConnectorError",
    "ConnectorHooks",
    "Event",
    "EventSource",
    "Surface",
    "SurfaceApi",
    "validate_config",
    "missing_required",
]
