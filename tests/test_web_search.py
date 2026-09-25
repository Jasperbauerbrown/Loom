"""`web_search` (03 §9.2): the four providers, the defined failures, and the policy it
lands on. No network (MockTransport), no live model, no real keyring (conftest)."""

from __future__ import annotations

import httpx
import pytest

from loom.tools import permissions
from loom.tools.native import web_search as ws
from loom.tools.registry import ToolContext


@pytest.fixture
def ctx(app):
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1", event_id=1)


@pytest.fixture
def serve(monkeypatch):
    """Install a handler for the provider call; returns the requests it saw."""
    seen: list[httpx.Request] = []

    def install(handler):
        def wrapped(request):
            seen.append(request)
            return handler(request)

        def factory(timeout):
            return httpx.AsyncClient(transport=httpx.MockTransport(wrapped), timeout=timeout)

        monkeypatch.setattr(ws, "client_factory", factory)

    return seen, install


def configure(app, provider, **rest):
    app.settings.set(app.db, "tools.web_search_provider", provider)
    for key, value in rest.items():
        app.settings.set(app.db, f"tools.{key}", value)
    if provider in ws.KEYED:
        app.secrets.set(str(app.settings.get("tools.web_search_secret")), "k-123")


async def search(app, ctx, **args):
    return await app.tools.get("web_search").handler(ctx, {"query": "loom local first", **args})


# ---------------------------------------------------------------------------------
# not configured
# ---------------------------------------------------------------------------------
async def test_without_a_provider_it_says_so_and_says_where(app, ctx):
    from loom import prompts

    result = await search(app, ctx)
    assert not result.ok
    assert prompts.load("context/web-search-unconfigured") in result.content
    assert "Settings" in result.content and "secret_set" in result.content


async def test_searxng_without_a_url_is_its_own_error(app, ctx):
    configure(app, "searxng")
    result = await search(app, ctx)
    assert not result.ok and "no instance URL is set" in result.content


async def test_a_keyed_provider_without_the_secret_names_the_secret(app, ctx):
    app.settings.set(app.db, "tools.web_search_provider", "brave")
    result = await search(app, ctx)
    assert not result.ok
    assert "'web_search'" in result.content and "secret_set" in result.content


# ---------------------------------------------------------------------------------
# the four providers
# ---------------------------------------------------------------------------------
async def test_searxng_queries_the_instance_and_renders_the_hits(app, ctx, serve):
    seen, install = serve
    configure(app, "searxng", web_search_url="http://127.0.0.1:8888/")
    install(lambda r: httpx.Response(200, json={"results": [
        {"title": "Loom", "url": "https://loom.example/a", "content": "a local-first\nassistant"},
        {"title": "Other", "url": "https://loom.example/b", "content": "second"},
    ]}))
    result = await search(app, ctx)
    assert result.ok and result.meta == {"count": 2, "provider": "searxng"}
    assert result.content.splitlines()[0] == '2 results for "loom local first" (searxng)'
    assert "1. Loom" in result.content and "https://loom.example/a" in result.content
    assert "a local-first assistant" in result.content  # snippet newlines collapsed
    assert "2. Other" in result.content
    assert str(seen[0].url) == "http://127.0.0.1:8888/search?q=loom+local+first&format=json"


async def test_brave_sends_the_subscription_token_and_reads_web_results(app, ctx, serve):
    seen, install = serve
    configure(app, "brave")
    install(lambda r: httpx.Response(200, json={"web": {"results": [
        {"title": "Brave hit", "url": "https://b.example/1", "description": "from brave"},
    ]}}))
    result = await search(app, ctx, k=3)
    assert result.ok and "Brave hit" in result.content and "from brave" in result.content
    assert seen[0].method == "GET"
    assert seen[0].headers["x-subscription-token"] == "k-123"
    assert "count=3" in str(seen[0].url)


async def test_exa_posts_the_query_with_the_api_key_header(app, ctx, serve):
    import json

    seen, install = serve
    configure(app, "exa")
    install(lambda r: httpx.Response(200, json={"results": [
        {"title": "Exa hit", "url": "https://e.example/1", "text": "from exa"},
    ]}))
    result = await search(app, ctx)
    assert result.ok and "Exa hit" in result.content and "from exa" in result.content
    assert seen[0].method == "POST" and seen[0].headers["x-api-key"] == "k-123"
    assert json.loads(seen[0].content)["query"] == "loom local first"


async def test_tavily_posts_with_a_bearer_token(app, ctx, serve):
    import json

    seen, install = serve
    configure(app, "tavily")
    install(lambda r: httpx.Response(200, json={"results": [
        {"title": "Tavily hit", "url": "https://t.example/1", "content": "from tavily"},
    ]}))
    result = await search(app, ctx)
    assert result.ok and "Tavily hit" in result.content
    assert seen[0].headers["authorization"] == "Bearer k-123"
    assert json.loads(seen[0].content)["max_results"] == 5  # the configured default


# ---------------------------------------------------------------------------------
# bounds and failures
# ---------------------------------------------------------------------------------
async def test_k_is_bounded_and_defaults_to_the_setting(app, ctx, serve):
    _, install = serve
    configure(app, "searxng", web_search_url="http://x", web_search_results=2)
    rows = [{"title": f"t{n}", "url": f"https://x/{n}", "content": ""} for n in range(30)]
    install(lambda r: httpx.Response(200, json={"results": rows}))
    assert (await search(app, ctx)).meta["count"] == 2
    assert (await search(app, ctx, k=4)).meta["count"] == 4
    assert (await search(app, ctx, k=500)).meta["count"] == ws.MAX_RESULTS


async def test_rows_without_a_url_are_dropped_and_snippets_are_capped(app, ctx, serve):
    _, install = serve
    configure(app, "searxng", web_search_url="http://x")
    install(lambda r: httpx.Response(200, json={"results": [
        {"title": "no url", "content": "x"},
        {"title": "", "url": "https://x/1", "content": "y" * 900},
    ]}))
    result = await search(app, ctx)
    assert result.meta["count"] == 1
    assert "https://x/1" in result.content and "no url" not in result.content
    assert "y" * ws.SNIPPET_CAP in result.content and "y" * (ws.SNIPPET_CAP + 1) not in result.content


async def test_no_hits_is_an_answer_not_a_failure(app, ctx, serve):
    _, install = serve
    configure(app, "searxng", web_search_url="http://x")
    install(lambda r: httpx.Response(200, json={"results": []}))
    result = await search(app, ctx)
    assert result.ok and result.content.startswith("no results for")
    assert result.meta["count"] == 0


async def test_a_provider_error_reports_the_status_and_the_first_body_line(app, ctx, serve):
    _, install = serve
    configure(app, "brave")
    install(lambda r: httpx.Response(429, text="rate limited, retry later\nmore detail"))
    result = await search(app, ctx)
    assert not result.ok and result.meta["status"] == 429
    assert "HTTP 429: rate limited, retry later" in result.content
    assert "more detail" not in result.content


async def test_a_network_failure_is_an_error_result(app, ctx, serve):
    _, install = serve
    configure(app, "searxng", web_search_url="http://x")

    def boom(request):
        raise httpx.ConnectError("no route to host", request=request)

    install(boom)
    result = await search(app, ctx)
    assert not result.ok and "ConnectError" in result.content


async def test_a_non_json_body_is_an_error_result(app, ctx, serve):
    _, install = serve
    configure(app, "searxng", web_search_url="http://x")
    install(lambda r: httpx.Response(200, text="<html>login</html>"))
    result = await search(app, ctx)
    assert not result.ok and "not JSON" in result.content


async def test_an_empty_query_is_rejected(app, ctx):
    configure(app, "searxng", web_search_url="http://x")
    result = await app.tools.get("web_search").handler(ctx, {"query": "  "})
    assert not result.ok and "query is empty" in result.content


# ---------------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------------
def test_it_is_registered_read_only_and_therefore_allowed(app):
    tool = app.tools.get("web_search")
    assert tool is not None and tool.read_only and tool.path_params == ()
    assert permissions.default_mode(tool) == "allow"
    assert permissions.decide(app, tool, {"query": "x"}) == ("allow", "web_search")
