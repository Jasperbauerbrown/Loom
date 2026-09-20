"""Fake model client: the test harness. Tests never call a live model (CLAUDE.md).

Scripting:
    fake = FakeModelClient()
    fake.script(
        Scripted(content="Heating.", tool_calls=[("shell", {"command": "curl ..."})]),
        Scripted(content="Done: heating to 200/60."),
    )
    fake.on(purpose="classify", respond=lambda req: Scripted(content='{"decision":"allow","reason":"in scope"}'))

Resolution order for each call: a matching `on(...)` responder for the request's
`purpose` → the next queued `Scripted` → `default` responder if set → an AssertionError
naming the unmatched purpose (so a test fails loudly rather than hanging).

Every request is recorded in `calls` for assertions. Deltas are emitted word by word so
streaming codepaths are exercised. `embed` is deterministic (hash-bucketed) so cosine
search is testable without a model.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from .types import (
    PRIORITY_INTERACTIVE,
    PRIORITY_MAINTENANCE,
    ChatRequest,
    Delta,
    ModelError,
    Outcome,
    Priority,
    Result,
    StreamEvent,
    ToolCall,
    Usage,
    collect,
)

Responder = Callable[[ChatRequest], "Scripted"]


@dataclass
class Scripted:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    outcome: Outcome = "ok"
    error: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    delay_s: float = 0.0  # simulate latency (used by cancel/priority tests)

    def to_result(self, call_index: int) -> Result:
        calls = [
            ToolCall(id=f"call_{call_index}_{i}", name=name, arguments=dict(args))
            for i, (name, args) in enumerate(self.tool_calls)
        ]
        finish = self.finish_reason or ("tool_calls" if calls else "stop")
        if self.outcome != "ok":
            finish = "error"
        return Result(
            outcome=self.outcome,
            content=self.content,
            reasoning=self.reasoning,
            tool_calls=calls,
            usage=Usage(self.prompt_tokens, self.completion_tokens),
            finish_reason=finish,
            error=self.error,
            model="fake",
        )


class FakeModelClient:
    def __init__(self, embedding_dim: int = 64, embeddings_available: bool = True):
        self.queue: list[Scripted] = []
        self.responders: dict[str, Responder] = {}
        self.default: Responder | None = None
        self.calls: list[ChatRequest] = []
        self.priorities: list[Priority] = []
        self.embedding_dim = embedding_dim
        self.embeddings_available = embeddings_available
        self.models: dict[str, list[str]] = {"local-ollama": ["fake-model", "nomic-embed-text"]}
        self._n = 0
        self.embed_calls: list[list[str]] = []

    # -- scripting --------------------------------------------------------------
    def script(self, *responses: Scripted) -> "FakeModelClient":
        self.queue.extend(responses)
        return self

    def on(self, purpose: str, respond: Responder) -> "FakeModelClient":
        self.responders[purpose] = respond
        return self

    def reply(self, content: str, **kw: Any) -> "FakeModelClient":
        return self.script(Scripted(content=content, **kw))

    def tool(self, name: str, args: dict[str, Any], content: str = "", **kw: Any) -> "FakeModelClient":
        return self.script(Scripted(content=content, tool_calls=[(name, args)], **kw))

    def _pick(self, request: ChatRequest) -> Scripted:
        if request.purpose in self.responders:
            return self.responders[request.purpose](request)
        if self.queue:
            return self.queue.pop(0)
        if self.default is not None:
            return self.default(request)
        raise AssertionError(
            f"FakeModelClient: no scripted response for purpose={request.purpose!r} "
            f"(call #{len(self.calls)}); last user message: {_last_user(request)!r}"
        )

    # -- ModelClient ------------------------------------------------------------
    async def stream(self, request: ChatRequest, priority: Priority = PRIORITY_INTERACTIVE) -> AsyncIterator[StreamEvent]:
        self.calls.append(request)
        self.priorities.append(priority)
        scripted = self._pick(request)
        self._n += 1
        if scripted.delay_s:
            await asyncio.sleep(scripted.delay_s)
        for word in _chunks(scripted.reasoning):
            yield Delta("reasoning", word)
        for word in _chunks(scripted.content):
            yield Delta("content", word)
        result = scripted.to_result(self._n)
        if result.usage.prompt_tokens is None and result.ok:
            result.usage.prompt_tokens = max(1, request.chars() // 4)
            result.usage.completion_tokens = max(1, len(scripted.content) // 4)
        yield result

    async def chat(self, request: ChatRequest, priority: Priority = PRIORITY_INTERACTIVE) -> Result:
        return await collect(self.stream(request, priority))

    async def embed(self, texts: list[str], priority: Priority = PRIORITY_MAINTENANCE) -> list[list[float]]:
        if not self.embeddings_available:
            raise ModelError("fatal", "no embedding preset configured")
        self.embed_calls.append(list(texts))
        return [_fake_vector(t, self.embedding_dim) for t in texts]

    async def list_models(self, provider: str) -> list[str]:
        if provider not in self.models:
            raise ModelError("fatal", f"unknown provider {provider!r}")
        return list(self.models[provider])

    def queue_state(self) -> dict[str, Any]:
        return {"depth": 0, "running": []}


def _chunks(text: str) -> list[str]:
    if not text:
        return []
    out, buf = [], ""
    for ch in text:
        buf += ch
        if ch in " \n":
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def _last_user(request: ChatRequest) -> str:
    for m in reversed(request.messages):
        if m.get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else str(c)
    return ""


def _fake_vector(text: str, dim: int) -> list[float]:
    """Bag-of-words hashed into `dim` buckets, L2-normalised. Similar texts → similar
    vectors, which is all the tests need."""
    vec = [0.0] * dim
    for tok in text.lower().split():
        h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=4).digest(), "big")
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]
