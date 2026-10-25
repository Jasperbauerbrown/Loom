"""Model client contract: request/response shapes shared by the real client, the fake
client, the runner, the classifier, maintenance and embeddings.

A model call is `client.stream(request, priority)`: an async iterator that yields zero
or more `Delta`s and then exactly one terminal `Result`. `client.chat(...)` collects the
same into one `Result` (its `content`/`reasoning` filled from the deltas).

Outcomes (03 §5.5): `ok`, `transient` (network, 5xx, malformed stream, empty
completion), `overflow` (context too long), `runaway` (reasoning used the whole output
budget or passed the preset's `thinking_cap` without acting), `fatal` (auth, model
missing, unknown provider). Retry policy lives in the runner, not here: the client reports, the runner
decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol, runtime_checkable

Priority = int  # 0 interactive, 1 background, 2 maintenance
PRIORITY_INTERACTIVE: Priority = 0
PRIORITY_BACKGROUND: Priority = 1
PRIORITY_MAINTENANCE: Priority = 2

Outcome = Literal["ok", "transient", "overflow", "runaway", "fatal"]
Channel = Literal["content", "reasoning"]

# Fixed per-image prompt-token estimate (03 §6.5): vision encoders spend roughly this
# for a screenshot-sized image; a preset's `image_tokens` overrides it and real-usage
# calibration corrects the drift. Honest at chars/4 precision.
DEFAULT_IMAGE_TOKENS = 1000


def image_tokens(params: dict[str, Any] | None) -> int:
    try:
        value = int((params or {}).get("image_tokens") or DEFAULT_IMAGE_TOKENS)
    except (TypeError, ValueError):
        value = DEFAULT_IMAGE_TOKENS
    return max(1, value)


def is_multimodal(params: dict[str, Any] | None) -> bool:
    value = (params or {}).get("multimodal")
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


@dataclass
class ChatRequest:
    """One resolved model request. `messages` and `tools` are in OpenAI chat shape
    (role/content/tool_calls/tool_call_id; tools as {"type":"function","function":{...}}).
    `params` is the merged generation config: provider defaults < preset < overrides."""

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    params: dict[str, Any] = field(default_factory=dict)
    preset: str = ""
    provider: str = ""
    model: str = ""
    # Who is asking; for logging and queue display only.
    conversation_id: str | None = None
    purpose: str = "turn"  # turn | classify | summarize | maintenance | status | test

    @property
    def max_tokens(self) -> int | None:
        v = self.params.get("max_tokens")
        return int(v) if v is not None else None

    @property
    def effective_ctx(self) -> int:
        return int(self.params.get("effective_ctx") or 32768)

    def chars(self) -> int:
        """Character count of the prompt as sent, for token calibration (03 §6.6). Images
        count as `image_tokens × 4` chars each, never as their base64."""
        import json

        per_image = image_tokens(self.params) * 4
        n = 0
        for m in self.messages:
            c = m.get("content")
            if isinstance(c, str):
                n += len(c)
            elif c is not None:
                n += len(json.dumps(c))
            for tc in m.get("tool_calls") or []:
                n += len(json.dumps(tc))
            n += len(m.get("images") or []) * per_image
        if self.tools:
            n += len(json.dumps(self.tools))
        return n


@dataclass
class Delta:
    channel: Channel
    text: str


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]  # parsed; `raw` keeps the original string if parsing failed
    raw: str = ""
    parse_error: str | None = None


@dataclass
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass
class Result:
    outcome: Outcome = "ok"
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""  # stop | length | tool_calls | error | cancelled
    error: str = ""  # human-readable, for system events and logs
    latency_ms: int = 0
    model: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


StreamEvent = Delta | Result


@dataclass
class Transcription:
    """One speech-to-text call through the `transcription` role (03 §5.7). Like a
    `Result`, it never comes back as an exception: `outcome` says what happened."""

    text: str = ""
    outcome: Outcome = "ok"
    error: str | None = None
    model: str = ""
    latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


@runtime_checkable
class ModelClient(Protocol):
    """The one interface every model consumer uses. Implemented by `models.client.Client`
    (real HTTP, priority queue) and `models.fake.FakeModelClient` (tests)."""

    async def stream(self, request: ChatRequest, priority: Priority = PRIORITY_INTERACTIVE) -> AsyncIterator[StreamEvent]: ...

    async def chat(self, request: ChatRequest, priority: Priority = PRIORITY_INTERACTIVE) -> Result: ...

    async def embed(self, texts: list[str], priority: Priority = PRIORITY_MAINTENANCE) -> list[list[float]]:
        """Embeddings through the embedding role; raises `ModelError(fatal)` if no
        embedding preset is configured. Vectors are L2-normalised."""
        ...

    async def transcribe(
        self, audio: bytes, mime: str, *, preset: str | None = None, priority: Priority = PRIORITY_INTERACTIVE
    ) -> "Transcription":
        """Speech to text through the transcription role (or `preset`). Never raises
        for a provider failure; the outcome is on the returned `Transcription`."""
        ...

    async def list_models(self, provider: str, role: str | None = None) -> list[str]:
        """Probe a provider for its live model list (GET /models?provider=&role=);
        `role="transcription"` asks for speech-to-text models where the provider can
        filter (OpenRouter's `output_modalities`)."""
        ...

    def queue_state(self) -> dict[str, Any]:
        """{"depth": int, "running": [{"provider","purpose","conversation_id","priority"}...]}."""
        ...


class ModelError(Exception):
    def __init__(self, outcome: Outcome, message: str):
        super().__init__(message)
        self.outcome: Outcome = outcome
        self.message = message


async def collect(stream: AsyncIterator[StreamEvent]) -> Result:
    """Drain a stream into a Result (used by `chat` implementations)."""
    content: list[str] = []
    reasoning: list[str] = []
    result: Result | None = None
    async for ev in stream:
        if isinstance(ev, Delta):
            (content if ev.channel == "content" else reasoning).append(ev.text)
        else:
            result = ev
    if result is None:
        result = Result(outcome="transient", error="stream ended without a result")
    if not result.content:
        result.content = "".join(content)
    if not result.reasoning:
        result.reasoning = "".join(reasoning)
    return result
