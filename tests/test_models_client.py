"""Model client: both wire shapes, the reasoning splitter, and one outcome per class.

Every transport call goes through httpx.MockTransport — no network, no live model."""

from __future__ import annotations

import json

import httpx
import pytest

from loom import db
from loom.models import roles
from loom.models.client import Client, ThinkSplitter, classify_http, messages_for, normalise
from loom.models.types import ChatRequest, Delta, ModelError, Result


# ---------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------
def ndjson(*chunks: dict) -> bytes:
    return b"".join(json.dumps(c).encode() + b"\n" for c in chunks)


def sse(*chunks: dict | str) -> bytes:
    out = b""
    for chunk in chunks:
        payload = chunk if isinstance(chunk, str) else json.dumps(chunk)
        out += b"data: " + payload.encode() + b"\n\n"
    return out


def make_client(app, handler, kind: str = "ollama") -> Client:
    if kind == "openai_compatible":
        now = db.now_iso()
        app.db.execute(
            "INSERT OR REPLACE INTO providers(name, kind, base_url, api_key_ref, params, enabled, created_at, updated_at) "
            "VALUES ('hosted', 'openai_compatible', 'https://api.example.test', NULL, '{}', 1, ?, ?)",
            (now, now),
        )
    client = Client(app, transport=httpx.MockTransport(handler))
    app.models = client
    return client


def request_for(provider: str = "local-ollama", **params) -> ChatRequest:
    merged = {"max_tokens": 256, "temperature": 0.2, "effective_ctx": 8192}
    merged.update(params)
    return ChatRequest(
        messages=[{"role": "user", "content": "hi"}],
        params=merged,
        provider=provider,
        preset="fake",
        model="test-model",
    )


async def drain(client: Client, request: ChatRequest, priority: int = 0) -> tuple[list[Delta], Result]:
    deltas: list[Delta] = []
    result: Result | None = None
    async for event in client.stream(request, priority):
        (deltas.append(event) if isinstance(event, Delta) else None)
        if isinstance(event, Result):
            result = event
    assert result is not None
    return deltas, result


# ---------------------------------------------------------------------------------
# ollama
# ---------------------------------------------------------------------------------
async def test_ollama_stream_thinking_content_and_tool_call(app):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=ndjson(
                {"model": "test-model", "message": {"role": "assistant", "thinking": "weighing "}},
                {"message": {"role": "assistant", "thinking": "options"}},
                {"message": {"role": "assistant", "content": "Heating"}},
                {"message": {"role": "assistant", "content": " now."}},
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{"function": {"name": "shell", "arguments": {"command": "ls"}}}],
                    }
                },
                {"done": True, "done_reason": "stop", "prompt_eval_count": 40, "eval_count": 7},
            ),
        )

    client = make_client(app, handler)
    deltas, result = await drain(client, request_for(reasoning="on", keep_alive="30m", num_ctx=4096))

    assert seen["url"].endswith("/api/chat")
    assert seen["body"]["stream"] is True and seen["body"]["think"] is True
    assert seen["body"]["options"] == {"num_ctx": 4096, "temperature": 0.2, "num_predict": 256}
    assert seen["body"]["keep_alive"] == "30m"

    assert result.outcome == "ok"
    assert result.content == "Heating now."
    assert result.reasoning == "weighing options"
    assert [d.channel for d in deltas] == ["reasoning", "reasoning", "content", "content"]
    assert result.tool_calls[0].name == "shell"
    assert result.tool_calls[0].arguments == {"command": "ls"}
    assert result.finish_reason == "tool_calls"
    assert result.usage.prompt_tokens == 40 and result.usage.completion_tokens == 7


async def test_ollama_tool_arguments_as_unparseable_string_set_parse_error(app):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=ndjson(
                {"message": {"tool_calls": [{"function": {"name": "shell", "arguments": "{not json"}}]}},
                {"done": True, "done_reason": "stop"},
            ),
        )

    client = make_client(app, handler)
    _, result = await drain(client, request_for())
    call = result.tool_calls[0]
    assert call.arguments == {} and call.parse_error and call.raw == "{not json"


async def test_reasoning_off_sends_think_false(app):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=ndjson({"message": {"content": "ok"}}, {"done": True}))

    client = make_client(app, handler)
    await drain(client, request_for(reasoning="off"))
    assert seen["body"]["think"] is False


# ---------------------------------------------------------------------------------
# openai-compatible
# ---------------------------------------------------------------------------------
async def test_openai_sse_fragmented_tool_arguments_and_usage(app):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=sse(
                {"model": "test-model", "choices": [{"delta": {"reasoning": "hm"}}]},
                {"choices": [{"delta": {"content": "Look"}}]},
                {"choices": [{"delta": {"content": "ing."}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "http", "arguments": '{"url":'}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"https://x"}'}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 3}},
                "[DONE]",
            ),
        )

    client = make_client(app, handler, kind="openai_compatible")
    deltas, result = await drain(client, request_for(provider="hosted"))

    assert seen["url"].endswith("/v1/chat/completions")
    assert seen["body"]["stream_options"] == {"include_usage": True}
    assert result.outcome == "ok" and result.content == "Looking."
    assert result.reasoning == "hm"
    assert [d.channel for d in deltas] == ["reasoning", "content", "content"]
    assert result.tool_calls[0].id == "c1"
    assert result.tool_calls[0].arguments == {"url": "https://x"}
    assert result.usage.prompt_tokens == 11 and result.usage.completion_tokens == 3
    assert result.finish_reason == "tool_calls"


async def test_openai_sends_bearer_token_from_secrets(app):
    seen: dict = {}

    class Secrets:
        def get(self, name: str) -> str:
            return "sk-test" if name == "openrouter_api_key" else ""

    app.secrets = Secrets()
    now = db.now_iso()
    app.db.execute(
        "INSERT OR REPLACE INTO providers(name, kind, base_url, api_key_ref, params, enabled, created_at, updated_at) "
        "VALUES ('hosted', 'openai_compatible', 'https://api.example.test', 'openrouter_api_key', '{}', 1, ?, ?)",
        (now, now),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}))

    client = Client(app, transport=httpx.MockTransport(handler))
    app.models = client
    await drain(client, request_for(provider="hosted"))
    assert seen["auth"] == "Bearer sk-test"


# ---------------------------------------------------------------------------------
# <think> splitter
# ---------------------------------------------------------------------------------
def test_think_splitter_handles_tags_split_across_chunks():
    splitter = ThinkSplitter()
    out: list[tuple[str, str]] = []
    for chunk in ["<th", "ink>plan", "ning</th", "ink>answer"]:
        out += splitter.feed(chunk)
    out += splitter.flush()
    assert [c for c, _ in out] == ["reasoning", "reasoning", "content"]
    assert "".join(t for c, t in out if c == "reasoning") == "planning"
    assert "".join(t for c, t in out if c == "content") == "answer"


def test_think_splitter_passes_plain_text_through():
    splitter = ThinkSplitter()
    assert splitter.feed("hello ") == [("content", "hello ")]
    assert splitter.flush() == []


async def test_inline_think_tags_in_ollama_content_become_reasoning(app):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=ndjson(
                {"message": {"content": "<thi"}},
                {"message": {"content": "nk>why</think>because"}},
                {"done": True, "done_reason": "stop"},
            ),
        )

    client = make_client(app, handler)
    _, result = await drain(client, request_for())
    assert result.reasoning == "why" and result.content == "because"


# ---------------------------------------------------------------------------------
# outcomes (03 §5.5)
# ---------------------------------------------------------------------------------
async def test_network_failure_is_transient(app):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = make_client(app, handler)
    _, result = await drain(client, request_for())
    assert result.outcome == "transient" and "ConnectError" in result.error


async def test_idle_timeout_is_transient(app):
    async def handler(request: httpx.Request) -> httpx.Response:
        import asyncio

        await asyncio.sleep(5)
        return httpx.Response(200, content=b"")

    client = make_client(app, handler)
    _, result = await drain(client, request_for(stream_idle_s=0.05, timeout_s=0.2))
    assert result.outcome == "transient"


async def test_401_is_fatal(app):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"})

    client = make_client(app, handler)
    _, result = await drain(client, request_for())
    assert result.outcome == "fatal" and "authentication" in result.error


async def test_400_mentioning_context_length_is_overflow(app):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "This model's maximum context length is 8192 tokens"})

    client = make_client(app, handler)
    _, result = await drain(client, request_for())
    assert result.outcome == "overflow"


async def test_finish_length_with_no_output_is_overflow(app):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ndjson({"message": {"content": ""}}, {"done": True, "done_reason": "length"}))

    client = make_client(app, handler)
    _, result = await drain(client, request_for())
    assert result.outcome == "overflow" and result.finish_reason == "length"


async def test_empty_completion_is_transient(app):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ndjson({"done": True, "done_reason": "stop"}))

    client = make_client(app, handler)
    _, result = await drain(client, request_for())
    assert result.outcome == "transient" and result.error == "empty completion"


async def test_unknown_provider_is_fatal(app):
    client = make_client(app, lambda request: httpx.Response(200, content=b""))
    _, result = await drain(client, request_for(provider="nope"))
    assert result.outcome == "fatal" and "unknown provider" in result.error


async def test_malformed_stream_is_transient(app):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not json at all}\n")

    client = make_client(app, handler)
    _, result = await drain(client, request_for())
    assert result.outcome == "transient" and "malformed stream" in result.error


def test_classify_http_table():
    assert classify_http(403, "nope")[0] == "fatal"
    assert classify_http(404, "model 'x' not found")[0] == "fatal"
    assert classify_http(429, "slow down")[0] == "transient"
    assert classify_http(503, "upstream")[0] == "transient"
    assert classify_http(400, "prompt is too long for the context window")[0] == "overflow"


# ---------------------------------------------------------------------------------
# background clamp, embeddings, model list, message rendering
# ---------------------------------------------------------------------------------
async def test_background_priority_clamps_max_tokens(app):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=ndjson({"message": {"content": "ok"}}, {"done": True}))

    app.settings.set(app.db, "models.background_max_tokens", 128)
    client = make_client(app, handler)
    await drain(client, request_for(max_tokens=4096), priority=1)
    assert seen["body"]["options"]["num_predict"] == 128

    await drain(client, request_for(max_tokens=4096), priority=0)
    assert seen["body"]["options"]["num_predict"] == 4096


async def test_embeddings_are_normalised(app):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/api/embed")
        return httpx.Response(200, json={"embeddings": [[3.0, 4.0], [0.0, 2.0]]})

    client = make_client(app, handler)
    app.db.execute("UPDATE model_roles SET preset='fake-embed' WHERE role='embedding'")
    vectors = await client.embed(["a", "b"])
    assert vectors[0] == [0.6, 0.8]
    assert abs(sum(v * v for v in vectors[1]) - 1.0) < 1e-9


async def test_embeddings_disabled_is_fatal(app):
    app.settings.set(app.db, "models.embeddings_enabled", False)
    client = make_client(app, lambda request: httpx.Response(200, json={}))
    with pytest.raises(ModelError) as exc:
        await client.embed(["a"])
    assert exc.value.outcome == "fatal"


async def test_list_models_ollama_and_openai(app):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/api/tags"):
            return httpx.Response(200, json={"models": [{"name": "qwen:7b"}, {"name": "nomic-embed-text:latest"}]})
        return httpx.Response(200, json={"data": [{"id": "gpt-x"}]})

    client = make_client(app, handler, kind="openai_compatible")
    assert await client.list_models("local-ollama") == ["nomic-embed-text:latest", "qwen:7b"]
    assert await client.list_models("hosted") == ["gpt-x"]


def test_messages_for_maps_reasoning_and_tool_arguments():
    messages = [
        {"role": "assistant", "content": "", "reasoning": "because", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": '{"command":"ls"}'}}
        ]},
        {"role": "assistant", "content": "plain", "reasoning": ""},
    ]
    ollama = messages_for("ollama", messages)
    assert ollama[0]["thinking"] == "because" and "reasoning" not in ollama[0]
    assert ollama[0]["tool_calls"][0]["function"]["arguments"] == {"command": "ls"}
    assert "thinking" not in ollama[1] and "reasoning" not in ollama[1]

    hosted = messages_for("openai_compatible", messages)
    assert hosted[0]["reasoning_content"] == "because"
    assert hosted[0]["tool_calls"][0]["function"]["arguments"] == '{"command":"ls"}'


def test_messages_for_never_sends_an_empty_tool_name():
    messages = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_0", "type": "function", "function": {"name": "", "arguments": "{}"}}
    ]}]
    for kind in ("ollama", "openai_compatible"):
        assert messages_for(kind, messages)[0]["tool_calls"][0]["function"]["name"] == "unnamed"
    assert messages[0]["tool_calls"][0]["function"]["name"] == ""  # canonical list untouched


def test_normalise_handles_the_zero_vector():
    assert normalise([0.0, 0.0]) == [0.0, 0.0]


# ---------------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------------
def test_resolve_merges_provider_then_preset_then_overrides(app):
    app.db.execute("UPDATE providers SET params=? WHERE name='local-ollama'", (db.dumps({"timeout_s": 1200, "concurrency": 2}),))
    resolved = roles.resolve(app, role="desk")
    assert resolved.params["timeout_s"] == 1200 and resolved.params["concurrency"] == 2
    assert resolved.params["max_tokens"] == 1024  # from the preset
    request = roles.build_request(app, role="desk", messages=[], overrides={"max_tokens": 32})
    assert request.params["max_tokens"] == 32 and request.model == "fake-model"


def test_utility_falls_back_to_desk(app):
    resolved = roles.resolve(app, role="utility")
    assert resolved.preset["name"] == "fake" and resolved.role == "desk"


def test_missing_role_preset_is_fatal_with_the_settings_url(app):
    app.db.execute("UPDATE model_roles SET preset=NULL WHERE role IN ('desk','worker')")
    with pytest.raises(ModelError) as exc:
        roles.resolve(app, role="worker")
    assert exc.value.outcome == "fatal" and "/settings/models" in exc.value.message


def test_preset_override_wins_over_role(app):
    resolved = roles.resolve(app, role="desk", preset="fake-embed")
    assert resolved.model == "nomic-embed-text"


def test_update_token_factor_seed_writes_the_preset(app):
    roles.update_token_factor_seed(app, "fake", 1.234567)
    assert roles.token_factor_seed(app, "fake") == 1.2346


async def test_test_preset_reports_outcome_through_the_client(app, fake):
    from loom.models import client as model_client
    from loom.models.fake import Scripted

    fake.script(Scripted(content="OK"))
    report = await model_client.test_preset(app, "fake")
    assert report["outcome"] == "ok" and report["content"] == "OK"
    assert report["usage"]["prompt_tokens"] is not None


async def test_test_preset_embeds_with_the_embedding_preset(app, fake):
    from loom.models import client as model_client

    report = await model_client.test_preset(app, "fake-embed")
    assert report["outcome"] == "ok" and "dimensions" in report["content"]
    assert fake.embed_calls and not report["error"]


async def test_test_preset_on_an_unknown_preset_is_a_fatal_report(app):
    from loom.models import client as model_client

    report = await model_client.test_preset(app, "nope")
    assert report["outcome"] == "fatal" and "nope" in report["error"]


async def test_probe_is_a_noop_when_providers_exist(app):
    from loom.models import client as model_client

    assert (await model_client.probe_local_ollama(app))["provider"] is None


def test_join_endpoint_does_not_double_the_version_prefix():
    from loom.models.client import join_endpoint

    assert join_endpoint("https://openrouter.ai/api/v1", "/v1/chat/completions") == "https://openrouter.ai/api/v1/chat/completions"
    assert join_endpoint("https://openrouter.ai/api/v1/", "/v1/models") == "https://openrouter.ai/api/v1/models"
    assert join_endpoint("https://openrouter.ai/api", "/v1/models") == "https://openrouter.ai/api/v1/models"
    assert join_endpoint("http://127.0.0.1:11434", "/api/chat") == "http://127.0.0.1:11434/api/chat"
    assert join_endpoint("https://host/V1", "/v1/embeddings") == "https://host/V1/embeddings"


async def test_openai_reasoning_off_sends_effort_none(app):
    """OpenRouter leaves a Qwen judge thinking unless told not to; `off` must be explicit
    (the classifier hit "length limit with no output" with max_tokens=200, 2026-09-21)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}, "[DONE]"),
        )

    client = make_client(app, handler, kind="openai_compatible")
    await drain(client, request_for("hosted", reasoning="off"))
    assert seen["body"]["reasoning"] == {"effort": "none"}
    await drain(client, request_for("hosted", reasoning="on"))
    assert seen["body"]["reasoning"] == {"effort": "medium"}
    await drain(client, request_for("hosted"))
    assert "reasoning" not in seen["body"]


async def test_sampling_penalties_reach_both_wire_shapes(app):
    """Penalties are ordinary preset params: Ollama options (repeat_penalty) and
    OpenAI-compatible body keys; absent params send nothing (a strict endpoint may
    reject repetition_penalty)."""
    seen: dict = {}

    def ollama(request: httpx.Request) -> httpx.Response:
        seen["ollama"] = json.loads(request.content)
        return httpx.Response(200, content=ndjson({"message": {"content": "ok"}}, {"done": True}))

    client = make_client(app, ollama)
    await drain(client, request_for(repetition_penalty=1.1, presence_penalty=0.3, frequency_penalty=0.2))
    opts = seen["ollama"]["options"]
    assert opts["repeat_penalty"] == 1.1 and opts["presence_penalty"] == 0.3 and opts["frequency_penalty"] == 0.2

    await drain(client, request_for())
    assert not {"repeat_penalty", "presence_penalty", "frequency_penalty"} & set(seen["ollama"].get("options", {}))

    def openai(request: httpx.Request) -> httpx.Response:
        seen["openai"] = json.loads(request.content)
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}, "[DONE]"))

    client = make_client(app, openai, kind="openai_compatible")
    await drain(client, request_for("hosted", presence_penalty=0.4, repetition_penalty=1.05))
    body = seen["openai"]
    assert body["presence_penalty"] == 0.4 and body["repetition_penalty"] == 1.05 and "frequency_penalty" not in body
    await drain(client, request_for("hosted"))
    assert not {"presence_penalty", "frequency_penalty", "repetition_penalty"} & set(seen["openai"])
