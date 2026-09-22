"""The real model client: two provider kinds, one streaming contract (03 §5).

`Client` implements `models.types.ModelClient`. It never raises for a model or network
failure — those are *expected inputs* (charter §2.1) and come back as a terminal
`Result` with an outcome the runner knows how to handle (03 §5.5):

    ok         content and/or tool calls arrived
    transient  network, timeout, 5xx, 429, malformed stream, empty completion
    overflow   context too long (4xx body says so, or finish=length with zero output)
    fatal      auth, unknown provider/preset, model missing, other 4xx

Provider kinds differ only in URL shape, auth and extras:
    ollama              POST /api/chat (NDJSON), /api/embed, GET /api/tags
    openai_compatible   POST /v1/chat/completions (SSE), /v1/embeddings, GET /v1/models

Reasoning arrives either as a dedicated field (`message.thinking`,
`delta.reasoning`/`delta.reasoning_content`) or inline as `<think>…</think>` in the
content stream; `ThinkSplitter` normalises both, tags split across chunk boundaries
included, so the runner sees one `reasoning` channel whatever the model does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from typing import Any, AsyncIterator, Iterable

import httpx

from .. import db, prompts
from . import roles
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
from .queue import ModelQueue

log = logging.getLogger(__name__)

OVERFLOW_HINTS = (
    "context length",
    "context window",
    "maximum context",
    "too many tokens",
    "too long",
    "token limit",
    "exceeds the context",
    "reduce the length",
    "prompt is too large",
)
DEFAULT_OLLAMA_HOSTS = ("http://127.0.0.1:11434",)


# ---------------------------------------------------------------------------------
# <think> splitter
# ---------------------------------------------------------------------------------
class ThinkSplitter:
    """Splits a content stream into (channel, text) pairs on `<think>`/`</think>`.

    Buffers only what could still be the start of a tag, so a tag arriving in pieces
    across chunks is handled by the same code as one arriving whole."""

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.inside = False

    def feed(self, text: str) -> list[tuple[str, str]]:
        if not text:
            return []
        self.buf += text
        out: list[tuple[str, str]] = []
        while True:
            tag = self.CLOSE if self.inside else self.OPEN
            channel = "reasoning" if self.inside else "content"
            idx = self.buf.find(tag)
            if idx >= 0:
                if idx:
                    out.append((channel, self.buf[:idx]))
                self.buf = self.buf[idx + len(tag) :]
                self.inside = not self.inside
                continue
            keep = _partial_suffix(self.buf, tag)
            emit = self.buf[: len(self.buf) - keep] if keep else self.buf
            if emit:
                out.append((channel, emit))
            self.buf = self.buf[len(self.buf) - keep :] if keep else ""
            break
        return out

    def flush(self) -> list[tuple[str, str]]:
        if not self.buf:
            return []
        out = [("reasoning" if self.inside else "content", self.buf)]
        self.buf = ""
        return out


def _partial_suffix(buf: str, tag: str) -> int:
    """Length of the longest suffix of `buf` that is a proper prefix of `tag`."""
    for n in range(min(len(buf), len(tag) - 1), 0, -1):
        if buf.endswith(tag[:n]):
            return n
    return 0


# ---------------------------------------------------------------------------------
# outcome mapping (03 §5.5)
# ---------------------------------------------------------------------------------
def classify_http(status: int, body: str, url: str | None = None) -> tuple[Outcome, str]:
    text = (body or "").strip()
    lowered = text.lower()
    detail = _first_line(text)
    if any(hint in lowered for hint in OVERFLOW_HINTS):
        return "overflow", f"context overflow (HTTP {status}): {detail}"
    if status in (401, 403):
        return "fatal", f"authentication failed (HTTP {status}): {detail}"
    if status == 404:
        # A 404 is nearly always a wrong base URL or model id; name the URL so the
        # person can see which without reading logs.
        where = f" at {url}" if url else ""
        return "fatal", f"model or endpoint not found (HTTP {status}){where}: {detail} — check the provider's base URL and the model id"
    if status in (408, 409, 425, 429):
        return "transient", f"provider busy (HTTP {status}): {detail}"
    if 400 <= status < 500:
        return "fatal", f"request rejected (HTTP {status}): {detail}"
    return "transient", f"provider error (HTTP {status}): {detail}"


def _first_line(text: str, limit: int = 400) -> str:
    cleaned = " ".join(text.split())
    return cleaned[:limit]


def _finalise(result: Result) -> Result:
    """Last outcome rules that depend on what actually came back."""
    if result.outcome != "ok":
        return result
    empty = not result.content.strip() and not result.tool_calls
    if empty and result.finish_reason == "length":
        result.outcome = "overflow"
        result.error = "generation hit the length limit with no output"
    elif empty:
        result.outcome = "transient"
        result.error = "empty completion"
    return result


# ---------------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------------
class Client:
    """One per App. Owns the httpx client and the per-provider priority queue."""

    def __init__(self, app, transport: httpx.AsyncBaseTransport | None = None):  # type: ignore[no-untyped-def]
        self.app = app
        self.queue = ModelQueue(app)
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    # -- plumbing ---------------------------------------------------------------
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            # Timeouts are enforced per call (provider params), not by httpx globally.
            self._http = httpx.AsyncClient(timeout=None, transport=self._transport, follow_redirects=True)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def queue_state(self) -> dict[str, Any]:
        return self.queue.state()

    def provider(self, name: str) -> dict[str, Any]:
        row = roles.provider_row(self.app, name)
        if row is None:
            raise ModelError("fatal", f"unknown provider {name!r}")
        return row

    def _api_key(self, provider: dict[str, Any]) -> str:
        ref = provider.get("api_key_ref") or ""
        if not ref or self.app.secrets is None:
            return ""
        try:
            return self.app.secrets.get(ref) or ""
        except Exception:
            log.warning("secret %r for provider %r could not be read", ref, provider.get("name"))
            return ""

    # -- ModelClient -------------------------------------------------------------
    async def stream(self, request: ChatRequest, priority: Priority = PRIORITY_INTERACTIVE) -> AsyncIterator[StreamEvent]:
        started = time.monotonic()
        try:
            provider = self.provider(request.provider)
        except ModelError as exc:
            yield Result(outcome=exc.outcome, error=exc.message, finish_reason="error", model=request.model)
            return

        params = self._effective_params(request, provider, priority)
        info = {
            "purpose": request.purpose,
            "conversation_id": request.conversation_id,
            "preset": request.preset,
            "model": request.model,
        }
        result: Result | None = None
        async with self.queue.slot(
            str(provider["name"]), int(priority), info, concurrency=int(params.get("concurrency") or 1)
        ):
            kind = str(provider["kind"])
            if kind == "ollama":
                gen = self._stream_ollama(request, provider, params)
            elif kind == "openai_compatible":
                gen = self._stream_openai(request, provider, params)
            else:
                yield Result(
                    outcome="fatal",
                    error=f"unknown provider kind {kind!r}",
                    finish_reason="error",
                    model=request.model,
                )
                return
            try:
                async for event in gen:
                    if isinstance(event, Result):
                        result = event
                    else:
                        yield event
            except ModelError as exc:
                result = Result(outcome=exc.outcome, error=exc.message, finish_reason="error")
            except (httpx.HTTPError, OSError) as exc:
                result = Result(outcome="transient", error=f"{type(exc).__name__}: {exc}", finish_reason="error")
            except (asyncio.TimeoutError, TimeoutError):
                result = Result(outcome="transient", error="model call timed out", finish_reason="error")
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                result = Result(outcome="transient", error=f"malformed stream: {exc}", finish_reason="error")
            finally:
                # A consumer that stops iterating closes the HTTP response with us.
                await gen.aclose()
        if result is None:
            result = Result(outcome="transient", error="stream ended without a result", finish_reason="error")
        result.model = result.model or request.model
        result.latency_ms = int((time.monotonic() - started) * 1000)
        yield _finalise(result)

    async def chat(self, request: ChatRequest, priority: Priority = PRIORITY_INTERACTIVE) -> Result:
        return await collect(self.stream(request, priority))

    async def embed(self, texts: list[str], priority: Priority = PRIORITY_MAINTENANCE) -> list[list[float]]:
        if not texts:
            return []
        if not self.app.settings.get("models.embeddings_enabled"):
            raise ModelError("fatal", "embeddings are disabled in settings")
        resolved = roles.resolve(self.app, role="embedding")
        provider = resolved.provider
        params = dict(resolved.params)
        info = {"purpose": "embed", "preset": resolved.preset["name"], "model": resolved.model}
        async with self.queue.slot(
            resolved.provider_name, int(priority), info, concurrency=int(params.get("concurrency") or 1)
        ):
            vectors = await self._embed_call(provider, params, resolved.model, list(texts))
        return [normalise(v) for v in vectors]

    async def list_models(self, provider: str) -> list[str]:
        row = self.provider(provider)
        params = roles.provider_params(row)
        url, key = self._endpoint(row, "/api/tags" if row["kind"] == "ollama" else "/v1/models")
        try:
            response = await self.http().get(url, headers=self._headers(row), timeout=_short(params))
        except (httpx.HTTPError, OSError) as exc:
            raise ModelError("transient", f"{provider}: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            outcome, message = classify_http(response.status_code, response.text, url=url)
            raise ModelError(outcome, f"{provider}: {message}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ModelError("transient", f"{provider}: malformed model list") from exc
        if row["kind"] == "ollama":
            names = [str(m.get("name") or m.get("model") or "") for m in payload.get("models") or []]
        else:
            names = [str(m.get("id") or "") for m in payload.get("data") or []]
        return sorted(n for n in names if n)

    # -- shared request shaping --------------------------------------------------
    def _effective_params(self, request: ChatRequest, provider: dict[str, Any], priority: int) -> dict[str, Any]:
        params = roles.provider_params(provider)
        params.update({k: v for k, v in (request.params or {}).items() if v is not None})
        if int(priority) >= 1:
            # 03 §5.3: a background generation is bounded so an interactive call waits at
            # most one of them.
            cap = int(self.app.settings.get("models.background_max_tokens"))
            current = params.get("max_tokens")
            params["max_tokens"] = cap if current is None else min(int(current), cap)
        return params

    def _endpoint(self, provider: dict[str, Any], path: str) -> tuple[str, str]:
        return join_endpoint(str(provider.get("base_url") or ""), path), self._api_key(provider)

    def _headers(self, provider: dict[str, Any]) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        key = self._api_key(provider)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    # -- ollama ------------------------------------------------------------------
    async def _stream_ollama(
        self, request: ChatRequest, provider: dict[str, Any], params: dict[str, Any]
    ) -> AsyncIterator[StreamEvent]:
        url, _ = self._endpoint(provider, "/api/chat")
        options: dict[str, Any] = {}
        num_ctx = params.get("num_ctx") or params.get("effective_ctx")
        if num_ctx:
            options["num_ctx"] = int(num_ctx)
        if params.get("temperature") is not None:
            options["temperature"] = float(params["temperature"])
        if params.get("top_p") is not None:
            options["top_p"] = float(params["top_p"])
        if params.get("max_tokens") is not None:
            options["num_predict"] = int(params["max_tokens"])
        # Sampling penalties: the standard lever against reasoning that repeats itself.
        if params.get("repetition_penalty") is not None:
            options["repeat_penalty"] = float(params["repetition_penalty"])
        if params.get("presence_penalty") is not None:
            options["presence_penalty"] = float(params["presence_penalty"])
        if params.get("frequency_penalty") is not None:
            options["frequency_penalty"] = float(params["frequency_penalty"])

        body: dict[str, Any] = {
            "model": request.model,
            "messages": messages_for("ollama", request.messages, multimodal=roles.is_multimodal(params)),
            "stream": True,
        }
        if request.tools:
            body["tools"] = request.tools
        if options:
            body["options"] = options
        if params.get("keep_alive"):
            body["keep_alive"] = params["keep_alive"]
        think = _ollama_think(params.get("reasoning"))
        if think is not None:
            body["think"] = think

        result = Result()
        splitter = ThinkSplitter()
        calls: list[ToolCall] = []
        async for line in self._lines(url, provider, body, params):
            if not line.strip():
                continue
            chunk = json.loads(line)
            if isinstance(chunk.get("error"), (str, dict)):
                error = chunk["error"] if isinstance(chunk["error"], str) else json.dumps(chunk["error"])
                outcome, message = classify_http(200, error)
                yield Result(outcome=outcome if outcome != "fatal" else "transient", error=message, finish_reason="error")
                return
            message = chunk.get("message") or {}
            thinking = message.get("thinking") or message.get("reasoning")
            if thinking:
                result.reasoning += thinking
                yield Delta("reasoning", thinking)
            content = message.get("content")
            if content:
                for channel, text in splitter.feed(content):
                    if channel == "content":
                        result.content += text
                    else:
                        result.reasoning += text
                    yield Delta(channel, text)  # type: ignore[arg-type]
            for raw in message.get("tool_calls") or []:
                calls.append(_ollama_tool_call(raw, len(calls)))
            if chunk.get("prompt_eval_count") is not None:
                result.usage.prompt_tokens = int(chunk["prompt_eval_count"])
            if chunk.get("eval_count") is not None:
                result.usage.completion_tokens = int(chunk["eval_count"])
            if chunk.get("model"):
                result.model = str(chunk["model"])
            if chunk.get("done"):
                result.finish_reason = _ollama_finish(str(chunk.get("done_reason") or ""), bool(calls))
        for channel, text in splitter.flush():
            if channel == "content":
                result.content += text
            else:
                result.reasoning += text
            yield Delta(channel, text)  # type: ignore[arg-type]
        result.tool_calls = calls
        if not result.finish_reason:
            result.finish_reason = "tool_calls" if calls else "stop"
        yield result

    # -- openai-compatible --------------------------------------------------------
    async def _stream_openai(
        self, request: ChatRequest, provider: dict[str, Any], params: dict[str, Any]
    ) -> AsyncIterator[StreamEvent]:
        url, _ = self._endpoint(provider, "/v1/chat/completions")
        body: dict[str, Any] = {
            "model": request.model,
            "messages": messages_for("openai_compatible", request.messages, multimodal=roles.is_multimodal(params)),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            body["tools"] = request.tools
        if params.get("max_tokens") is not None:
            body["max_tokens"] = int(params["max_tokens"])
        if params.get("temperature") is not None:
            body["temperature"] = float(params["temperature"])
        if params.get("top_p") is not None:
            body["top_p"] = float(params["top_p"])
        # Penalties are sent only when the preset sets them: `repetition_penalty` is an
        # OpenRouter/vLLM/llama.cpp extension that a strict OpenAI endpoint may reject.
        for key in ("presence_penalty", "frequency_penalty", "repetition_penalty"):
            if params.get(key) is not None:
                body[key] = float(params[key])
        reasoning = _openai_reasoning(params.get("reasoning"))
        if reasoning is not None:
            body["reasoning"] = reasoning

        result = Result()
        splitter = ThinkSplitter()
        fragments: dict[int, dict[str, Any]] = {}
        async for line in self._lines(url, provider, body, params):
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            chunk = json.loads(payload)
            if isinstance(chunk.get("error"), (str, dict)):
                error = chunk["error"] if isinstance(chunk["error"], str) else json.dumps(chunk["error"])
                outcome, message = classify_http(200, error)
                yield Result(outcome=outcome if outcome != "fatal" else "transient", error=message, finish_reason="error")
                return
            if chunk.get("model"):
                result.model = str(chunk["model"])
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                if usage.get("prompt_tokens") is not None:
                    result.usage.prompt_tokens = int(usage["prompt_tokens"])
                if usage.get("completion_tokens") is not None:
                    result.usage.completion_tokens = int(usage["completion_tokens"])
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                thinking = delta.get("reasoning") or delta.get("reasoning_content")
                if thinking:
                    result.reasoning += thinking
                    yield Delta("reasoning", thinking)
                content = delta.get("content")
                if content:
                    for channel, text in splitter.feed(content):
                        if channel == "content":
                            result.content += text
                        else:
                            result.reasoning += text
                        yield Delta(channel, text)  # type: ignore[arg-type]
                for fragment in delta.get("tool_calls") or []:
                    _merge_openai_fragment(fragments, fragment)
                if choice.get("finish_reason"):
                    result.finish_reason = str(choice["finish_reason"])
        for channel, text in splitter.flush():
            if channel == "content":
                result.content += text
            else:
                result.reasoning += text
            yield Delta(channel, text)  # type: ignore[arg-type]
        result.tool_calls = [_openai_tool_call(f, i) for i, f in enumerate(_ordered(fragments))]
        if not result.finish_reason:
            result.finish_reason = "tool_calls" if result.tool_calls else "stop"
        yield result

    # -- transport ----------------------------------------------------------------
    async def _lines(
        self, url: str, provider: dict[str, Any], body: dict[str, Any], params: dict[str, Any]
    ) -> AsyncIterator[str]:
        """POST `body` and yield response lines, enforcing the idle and total timeouts.
        Closing the consumer closes the HTTP response (cancellation, 03 §6.4)."""
        total = float(params.get("timeout_s") or roles.DEFAULT_TIMEOUT_S)
        idle = float(params.get("stream_idle_s") or roles.DEFAULT_STREAM_IDLE_S)
        deadline = time.monotonic() + total
        headers = self._headers(provider)
        headers["Accept"] = "text/event-stream, application/x-ndjson, application/json"
        request = self.http().build_request("POST", url, json=body, headers=headers)
        response = await asyncio.wait_for(
            self.http().send(request, stream=True), timeout=max(0.001, min(idle, deadline - time.monotonic()))
        )
        try:
            if response.status_code >= 400:
                text = (await response.aread()).decode("utf-8", "replace")
                outcome, message = classify_http(response.status_code, text, url=url)
                raise ModelError(outcome, message)
            iterator = response.aiter_lines().__aiter__()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ModelError("transient", f"model call exceeded timeout_s ({total:.0f}s)")
                try:
                    line = await asyncio.wait_for(iterator.__anext__(), timeout=min(idle, remaining))
                except StopAsyncIteration:
                    return
                except (asyncio.TimeoutError, TimeoutError):
                    raise ModelError("transient", f"no data for {idle:.0f}s (stream_idle_s)") from None
                yield line
        finally:
            await response.aclose()

    async def _embed_call(
        self, provider: dict[str, Any], params: dict[str, Any], model: str, texts: list[str]
    ) -> list[list[float]]:
        kind = str(provider["kind"])
        if kind == "ollama":
            url, _ = self._endpoint(provider, "/api/embed")
            body: dict[str, Any] = {"model": model, "input": texts}
            if params.get("keep_alive"):
                body["keep_alive"] = params["keep_alive"]
        elif kind == "openai_compatible":
            url, _ = self._endpoint(provider, "/v1/embeddings")
            body = {"model": model, "input": texts}
        else:
            raise ModelError("fatal", f"unknown provider kind {kind!r}")
        try:
            response = await self.http().post(url, json=body, headers=self._headers(provider), timeout=_short(params))
        except (httpx.HTTPError, OSError) as exc:
            raise ModelError("transient", f"embedding call failed: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            outcome, message = classify_http(response.status_code, response.text, url=url)
            raise ModelError(outcome, message)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ModelError("transient", "malformed embedding response") from exc
        if kind == "ollama":
            vectors = payload.get("embeddings") or []
        else:
            vectors = [item.get("embedding") or [] for item in payload.get("data") or []]
        if len(vectors) != len(texts):
            raise ModelError("transient", f"embedding count mismatch: asked {len(texts)}, got {len(vectors)}")
        return [[float(x) for x in vec] for vec in vectors]


# ---------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------
def messages_for(kind: str, messages: list[dict[str, Any]], multimodal: bool = False) -> list[dict[str, Any]]:
    """Render the canonical message list into a provider's wire shape.

    The canonical shape is OpenAI's, plus a `reasoning` key on assistant messages for
    in-turn reasoning replay (03 §6.3) and an in-memory `images` key (data URLs) on user
    messages. Each kind names the reasoning field differently, and disagrees about
    whether `tool_calls[].function.arguments` is an object or a JSON string; both are one
    transformation here rather than a special case at each caller. An empty or absent
    `reasoning` is omitted — never sent as "".

    Images are the only place `content` stops being a string, and only on the wire: a
    multimodal OpenAI-compatible call gets a parts array, Ollama gets its `images` array
    of bare base64, and a text-only preset gets one placeholder line per image (from
    `prompts/context/image-placeholder.md`) so a strict chat template never sees a list.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        rendered = dict(message)
        images = [u for u in (rendered.pop("images", None) or []) if isinstance(u, str) and u]
        reasoning = rendered.pop("reasoning", None)
        if reasoning and rendered.get("role") == "assistant":
            rendered["thinking" if kind == "ollama" else "reasoning_content"] = reasoning
        calls = rendered.get("tool_calls")
        if calls:
            rendered["tool_calls"] = [_render_call(kind, call) for call in calls]
        if images:
            text = rendered.get("content") if isinstance(rendered.get("content"), str) else ""
            if not multimodal:
                note = prompts.load("context/image-placeholder")
                rendered["content"] = "\n".join(([text] if text else []) + [note] * len(images))
            elif kind == "ollama":
                rendered["content"] = text
                rendered["images"] = [_bare_base64(u) for u in images]
            else:
                parts: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
                parts.extend({"type": "image_url", "image_url": {"url": u}} for u in images)
                rendered["content"] = parts
        out.append(rendered)
    return out


def _bare_base64(url: str) -> str:
    """Ollama wants the base64 payload without the `data:<mime>;base64,` prefix."""
    if url.startswith("data:") and ";base64," in url:
        return url.split(";base64,", 1)[1]
    return url


def _render_call(kind: str, call: dict[str, Any]) -> dict[str, Any]:
    rendered = dict(call)
    function = dict(rendered.get("function") or {})
    if "arguments" in function:
        arguments = function["arguments"]
        if kind == "ollama" and isinstance(arguments, str):
            try:
                parsed = json.loads(arguments or "{}")
            except ValueError:
                parsed = {}
            function["arguments"] = parsed if isinstance(parsed, dict) else {}
        elif kind != "ollama" and not isinstance(arguments, str):
            function["arguments"] = json.dumps(arguments or {}, sort_keys=True)
        rendered["function"] = function
    return rendered


def _short(params: dict[str, Any]) -> float:
    """Timeout for non-streaming calls: the stream-idle budget is the right order of
    magnitude (one response, no generation loop)."""
    return float(params.get("stream_idle_s") or roles.DEFAULT_STREAM_IDLE_S)


def _ollama_think(reasoning: Any) -> bool | None:
    if reasoning in (None, ""):
        return None
    value = str(reasoning)
    if value == "off":
        return False
    return True  # "on" and "budget:<n>": Ollama has no budget knob, only on/off.


def _openai_reasoning(reasoning: Any) -> dict[str, Any] | None:
    if reasoning in (None, ""):
        return None
    value = str(reasoning)
    if value == "off":
        # OpenRouter's unified knob: `effort: none` is what stops a model that reasons by
        # default (Qwen 3.x). Omitting the parameter leaves it thinking, and a small
        # max_tokens then buys reasoning and no answer — the permission judge and the
        # maintenance pass both hit "length limit with no output" that way (2026-09-21).
        return {"effort": "none"}
    if value.startswith("budget:"):
        try:
            return {"max_tokens": int(value.split(":", 1)[1])}
        except ValueError:
            return {"effort": "medium"}
    if value in ("low", "medium", "high"):
        return {"effort": value}
    return {"effort": "medium"}


def _ollama_finish(done_reason: str, has_calls: bool) -> str:
    if done_reason in ("length", "limit"):
        return "length"
    if has_calls:
        return "tool_calls"
    return done_reason or "stop"


def _ollama_tool_call(raw: dict[str, Any], index: int) -> ToolCall:
    function = raw.get("function") or {}
    arguments = function.get("arguments")
    return _tool_call(
        call_id=str(raw.get("id") or f"call_{index}"),
        name=str(function.get("name") or ""),
        arguments=arguments,
    )


def _tool_call(call_id: str, name: str, arguments: Any) -> ToolCall:
    if isinstance(arguments, dict):
        return ToolCall(id=call_id, name=name, arguments=arguments, raw=json.dumps(arguments, sort_keys=True))
    raw = arguments if isinstance(arguments, str) else ("" if arguments is None else str(arguments))
    if not raw.strip():
        return ToolCall(id=call_id, name=name, arguments={}, raw=raw)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        return ToolCall(id=call_id, name=name, arguments={}, raw=raw, parse_error=str(exc))
    if not isinstance(parsed, dict):
        return ToolCall(id=call_id, name=name, arguments={}, raw=raw, parse_error="arguments are not an object")
    return ToolCall(id=call_id, name=name, arguments=parsed, raw=raw)


def _merge_openai_fragment(fragments: dict[int, dict[str, Any]], fragment: dict[str, Any]) -> None:
    index = int(fragment.get("index") or 0)
    slot = fragments.setdefault(index, {"id": "", "name": "", "arguments": ""})
    if fragment.get("id"):
        slot["id"] = str(fragment["id"])
    function = fragment.get("function") or {}
    if function.get("name"):
        slot["name"] = str(function["name"])
    if function.get("arguments"):
        slot["arguments"] += str(function["arguments"])


def _ordered(fragments: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [fragments[i] for i in sorted(fragments)]


def _openai_tool_call(fragment: dict[str, Any], index: int) -> ToolCall:
    return _tool_call(
        call_id=str(fragment.get("id") or f"call_{index}"),
        name=str(fragment.get("name") or ""),
        arguments=fragment.get("arguments") or "",
    )


def normalise(vector: Iterable[float]) -> list[float]:
    """L2-normalise so cosine search is a dot product (03 §5.6)."""
    values = [float(x) for x in vector]
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        return values
    return [v / norm for v in values]


# ---------------------------------------------------------------------------------
# lifecycle + operations used by the server
# ---------------------------------------------------------------------------------
async def start(app) -> None:  # type: ignore[no-untyped-def]
    """Called from the server lifespan after `loom.app.start`."""
    await probe_local_ollama(app)


async def stop(app) -> None:  # type: ignore[no-untyped-def]
    client = getattr(app, "models", None)
    aclose = getattr(client, "aclose", None)
    if aclose is not None:
        try:
            await aclose()
        except Exception:
            log.exception("closing the model client failed")


async def probe_local_ollama(app) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """First run (03 §3.3 step 2): if nothing is configured, look for a local Ollama and
    write what we find. Never raises — a machine without Ollama is an ordinary case, the
    Setup screen just shows an empty provider list."""
    found: dict[str, Any] = {"provider": None, "models": [], "embedding": None}
    try:
        if app.db.execute("SELECT COUNT(*) FROM providers").fetchone()[0] > 0:
            return found
        for base in _candidate_ollama_hosts():
            models = await _try_ollama(app, base)
            if models is None:
                continue
            now = db.now_iso()
            with db.transaction(app.db):
                app.db.execute(
                    "INSERT OR IGNORE INTO providers(name, kind, base_url, params, enabled, created_at, updated_at) "
                    "VALUES ('local-ollama', 'ollama', ?, '{}', 1, ?, ?)",
                    (base, now, now),
                )
            found["provider"] = "local-ollama"
            found["models"] = models
            embed_model = next((m for m in models if m.split(":")[0] == "nomic-embed-text"), None)
            if embed_model:
                with db.transaction(app.db):
                    app.db.execute(
                        "INSERT OR IGNORE INTO presets(name, provider, model, params, enabled, created_at, updated_at) "
                        "VALUES ('nomic-embed-text', 'local-ollama', ?, '{}', 1, ?, ?)",
                        (embed_model, now, now),
                    )
                    app.db.execute(
                        "UPDATE model_roles SET preset='nomic-embed-text' WHERE role='embedding' AND preset IS NULL"
                    )
                found["embedding"] = "nomic-embed-text"
            log.info("probed local Ollama at %s: %d models", base, len(models))
            app.bus.emit("models.changed", {"kind": "provider", "name": "local-ollama"})
            break
    except Exception:
        log.exception("ollama probe failed")
    return found


def _candidate_ollama_hosts() -> list[str]:
    hosts = list(DEFAULT_OLLAMA_HOSTS)
    raw = (os.environ.get("OLLAMA_HOST") or "").strip()
    if raw:
        url = raw if "://" in raw else f"http://{raw}"
        if url.rstrip("/") not in hosts:
            hosts.append(url.rstrip("/"))
    return hosts


async def _try_ollama(app, base: str) -> list[str] | None:  # type: ignore[no-untyped-def]
    try:
        async with httpx.AsyncClient(timeout=2.0) as http:
            response = await http.get(base.rstrip("/") + "/api/tags")
        if response.status_code >= 400:
            return None
        payload = response.json()
    except Exception:
        return None
    return sorted(str(m.get("name") or m.get("model") or "") for m in payload.get("models") or [])


async def test_preset(app, name: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """One short completion against a preset, for `POST /api/presets/{name}/test`.
    Goes through the ordinary client so the fake client answers it in tests."""
    from .. import prompts

    try:
        request = roles.build_request(
            app,
            preset=name,
            messages=[{"role": "user", "content": prompts.load("preset-test")}],
            overrides={"max_tokens": 64},
            purpose="test",
        )
    except ModelError as exc:
        return {"latency_ms": 0, "usage": {}, "content": "", "outcome": exc.outcome, "error": exc.message}
    started = time.monotonic()
    result = await app.models.chat(request, PRIORITY_INTERACTIVE)
    latency = result.latency_ms or int((time.monotonic() - started) * 1000)
    return {
        "latency_ms": latency,
        "usage": {
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
        },
        "content": result.content,
        "outcome": result.outcome,
        "error": result.error,
        "model": result.model,
    }


def join_endpoint(base_url: str, path: str) -> str:
    """Join a provider base URL and an API path without doubling a version prefix.

    People paste the base URL a provider documents — `https://openrouter.ai/api/v1`,
    `https://api.openai.com/v1` — and the client's paths already start with `/v1/`.
    Either form of base works: a trailing `/v1` is not repeated (2026-09-21)."""
    base = base_url.strip().rstrip("/")
    if path.startswith("/v1/") and base.lower().endswith("/v1"):
        path = path[len("/v1"):]
    return base + path
