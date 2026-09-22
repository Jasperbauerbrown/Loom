"""Tool contract and registry (03 §9.1).

A `Tool` is a name, a description (loaded from `prompts/tools/<name>.md` for native
tools), a JSON-schema `parameters` object, MCP-standard annotations, the argument names
the filesystem guard checks, a timeout, and an async handler.

Handlers receive `(ctx: ToolContext, args: dict)` and return a `ToolResult`. They never
raise for expected failures; the dispatcher (`tools/dispatch.py`) converts any exception
into an error result anyway. Output bounding and artifact spilling are the dispatcher's
job; a handler returns the full text.

Registry membership: native tools (registered at app build), MCP tools (prefixed
`mcp_<server>_<tool>`), connector tools (prefixed `<connector>_<tool>`). `source`
records which, so the Settings permissions table can group them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from ..app import App


@dataclass
class ToolResult:
    content: str
    ok: bool = True
    meta: dict[str, Any] = field(default_factory=dict)  # small structured extras (exit_code, status, ...)
    # Images the tool returned, each {"data": bytes, "mime": str}. The dispatcher stores
    # them as image artifacts and replaces this with refs in meta["images"]; bytes never
    # reach the transcript.
    images: list[dict[str, Any]] | None = None

    @staticmethod
    def error(message: str, **meta: Any) -> "ToolResult":
        return ToolResult(content=f"error: {message}", ok=False, meta=meta)


@dataclass
class ToolContext:
    """What a handler may see. Built by the dispatcher per call."""

    app: "App"
    conversation: dict[str, Any]  # the conversations row as a dict
    call_id: str
    event_id: int | None = None  # the tool_call event, once persisted
    request_text: str = ""  # the claimed user text / thread goal, for classify and logs
    priority: int = 0  # model-queue priority of the turn that issued the call (0 interactive)
    meta: dict[str, Any] = field(default_factory=dict)  # dispatcher writes {policy, decision, reason}
    extras: dict[str, Any] = field(default_factory=dict)  # per-turn scratch (tool_search's "exposed")

    @property
    def conversation_id(self) -> str:
        return self.conversation["id"]

    def roots(self) -> list[str]:
        """Effective allowed roots: global roots + the conversation's own."""
        from .fs_guard import effective_roots

        return effective_roots(self.app, self.conversation)


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema object: {"type":"object","properties":{...},"required":[...]}
    handler: Handler
    read_only: bool = False  # MCP readOnlyHint
    read_only_for: Callable[[dict[str, Any]], bool] | None = None  # per-call read-only test (http GET/HEAD)
    destructive: bool = False  # MCP destructiveHint
    path_params: tuple[str, ...] = ()  # args the fs guard resolves and checks
    timeout_s: float | None = None  # None → dispatcher picks by source/kind
    source: str = "native"  # native | mcp:<server> | connector:<name>
    deferred: bool = False  # true when hidden behind tool_search this turn (set by registry.expose)
    # A tool that pages its own output to `conversation.tool_output_cap` (fs_read,
    # knowledge_read, artifact_recall). The dispatcher never spills a paged tool's result: spilling a
    # page re-created the artifact it was reading, and a model could never get past the
    # first page (the Godot-debug loop, 2026-09-22).
    paged: bool = False
    # Conversation kinds that see the tool. Thread-only tools (conversation_finish,
    # status_update) are not offered to the desk, which otherwise called them and was
    # told "the desk never finishes" (2026-09-22).
    kinds: tuple[str, ...] = ("desk", "thread")

    def is_read_only(self, args: dict[str, Any] | None = None) -> bool:
        """Effective read-only for one call: the static hint, or the per-call test."""
        if self.read_only:
            return True
        if self.read_only_for is None:
            return False
        try:
            return bool(self.read_only_for(args or {}))
        except Exception:
            return False

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "read_only": self.read_only,
            "read_only_for": self.read_only_for is not None,
            "destructive": self.destructive,
            "path_params": list(self.path_params),
            "timeout_s": self.timeout_s,
            "source": self.source,
        }


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def unregister_source(self, source: str) -> list[str]:
        gone = [n for n, t in self._tools.items() if t.source == source]
        for n in gone:
            del self._tools[n]
        return gone

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def all(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def expose(self, limit: int, extra: set[str] | None = None) -> list[Tool]:
        """Tools to put in the model's `tools` parameter this turn: all of them if the
        count is within `limit`, otherwise native tools plus any names in `extra` (loaded
        by tool_search earlier in the turn). Sorted by name for a byte-stable prefix."""
        tools = self.all()
        if len(tools) <= limit:
            return tools
        extra = extra or set()
        return [t for t in tools if t.source == "native" or t.name in extra]

    def search(self, query: str, k: int = 10) -> list[Tool]:
        """Cheap substring/word match over name + description for `tool_search`."""
        q = query.lower().split()
        scored = []
        for t in self.all():
            hay = f"{t.name} {t.description}".lower()
            score = sum(hay.count(w) for w in q)
            if score:
                scored.append((score, t))
        scored.sort(key=lambda s: (-s[0], s[1].name))
        return [t for _, t in scored[:k]]
