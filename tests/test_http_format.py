"""`http` and its `format` argument (03 §9.2): raw / text / markdown over an HTML body,
and the promise that everything else comes back untouched. Nothing leaves the box — the
client factory is replaced with one on a MockTransport."""

from __future__ import annotations

import httpx
import pytest

from loom.tools.native import http as http_tool
from loom.tools.registry import ToolContext

PAGE = """<!doctype html>
<html><head><title>T</title><style>body{color:red}</style></head>
<body>
<nav><a href="/skip">skip me</a></nav>
<h1>Title</h1>
<p>Hello <strong>world</strong> and <a href="https://example.com/x">a link</a>.</p>
<ul><li>one</li><li>two</li></ul>
<script>var noise = 1;</script>
</body></html>"""


@pytest.fixture
def ctx(app):
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1", event_id=1)


@pytest.fixture
def serve(monkeypatch):
    """Install a response for the next request and return the requests seen."""
    seen: list[httpx.Request] = []

    def install(response: httpx.Response):
        def handler(request):
            seen.append(request)
            return response

        def factory(timeout, follow_redirects):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

        monkeypatch.setattr(http_tool, "client_factory", factory)

    return seen, install


async def get(app, ctx, **args):
    return await app.tools.get("http").handler(ctx, {"method": "GET", "url": "https://example.com/", **args})


def body_of(result) -> str:
    return result.content.split("\n\n", 1)[1]


async def test_html_comes_back_as_markdown_by_default(app, ctx, serve):
    _, install = serve
    install(httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"}))
    result = await get(app, ctx)
    body = body_of(result)
    assert result.content.startswith("HTTP 200 text/html")  # the header lines are unchanged
    assert "# Title" in body
    assert "**world**" in body
    assert "[a link](https://example.com/x)" in body
    assert "- one" in body and "- two" in body
    assert "var noise" not in body and "color:red" not in body  # script/style dropped
    assert "skip me" not in body  # nav dropped
    assert "<p>" not in body
    assert result.meta["format"] == "markdown"


async def test_text_format_strips_to_prose(app, ctx, serve):
    _, install = serve
    install(httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}))
    body = body_of(await get(app, ctx, format="text"))
    assert "Title" in body and "Hello world" in body
    assert "#" not in body and "**" not in body and "](" not in body
    assert "<" not in body


async def test_raw_format_returns_the_markup(app, ctx, serve):
    _, install = serve
    install(httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}))
    result = await get(app, ctx, format="raw")
    assert body_of(result) == PAGE
    assert result.meta["format"] == "raw"


async def test_untyped_html_is_still_converted(app, ctx, serve):
    """A server that declares nothing still gets its page read, or the whole budget goes
    on markup."""
    _, install = serve
    install(httpx.Response(200, content=PAGE.encode(), headers={"content-type": ""}))
    assert "# Title" in body_of(await get(app, ctx))


async def test_json_is_pretty_printed_in_every_format(app, ctx, serve):
    for fmt in ("markdown", "text", "raw"):
        _, install = serve
        install(httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"}))
        assert '"ok": true' in body_of(await get(app, ctx, format=fmt))


async def test_non_html_bodies_pass_through_unchanged(app, ctx, serve):
    source = "def f():\n    return '<b>not html</b>'\n"
    for fmt in ("markdown", "text", "raw"):
        _, install = serve
        install(httpx.Response(200, text=source, headers={"content-type": "text/x-python"}))
        assert body_of(await get(app, ctx, format=fmt)) == source


async def test_an_unknown_format_is_an_error_result(app, ctx):
    result = await get(app, ctx, format="pdf")
    assert not result.ok and "unsupported format pdf" in result.content


async def test_format_is_in_the_schema_with_markdown_as_the_default(app):
    schema = app.tools.get("http").parameters["properties"]["format"]
    assert schema["enum"] == ["raw", "text", "markdown"] and schema["default"] == "markdown"
