"""MCP client and transports (03 §10.1, §18 "MCP framing against an in-process fake").

The stdio tests spawn `tests/fake_mcp_server.py` as a real subprocess — framing is only
proved against a real pipe. The HTTP tests use `httpx.MockTransport` for both response
shapes the streamable-HTTP spec allows: one JSON message, or an SSE stream.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from loom.connectors.mcp.client import McpClient, flatten_content
from loom.connectors.mcp.transports import HttpTransport, McpError, StdioTransport

SERVER = str(Path(__file__).parent / "fake_mcp_server.py")


def stdio(mode: str = "normal") -> StdioTransport:
    return StdioTransport(sys.executable, [SERVER, mode])


async def connected(mode: str = "normal") -> McpClient:
    client = McpClient(stdio(mode), name="fake")
    await client.initialize(timeout=30)
    return client


# ---------------------------------------------------------------------------------
# stdio
# ---------------------------------------------------------------------------------
async def test_initialize_and_list_tools():
    client = await connected()
    try:
        assert client.initialized
        assert client.server_info["name"] == "fake-normal"
        assert client.capabilities.get("tools", {}).get("listChanged") is True
        tools = await client.list_tools(timeout=30)
        assert [t["name"] for t in tools] == ["fake_read", "fake_echo"]
        assert tools[0]["annotations"]["readOnlyHint"] is True
    finally:
        await client.close()


async def test_call_tool_text_and_image_parts():
    client = await connected()
    try:
        out = await client.call_tool("fake_read", {"path": "/tmp/x"}, timeout=30)
        assert out["ok"] is True
        assert out["content"] == "contents of /tmp/x"

        out = await client.call_tool("fake_echo", {"text": "hi"}, timeout=30)
        assert json.loads(out["content"].splitlines()[0]) == {"text": "hi"}
        assert "[image image/png, 5 bytes]" in out["content"]
    finally:
        await client.close()


async def test_call_tool_is_error_is_a_result_not_an_exception():
    client = await connected()
    try:
        out = await client.call_tool("fake_echo", {"fail": True}, timeout=30)
        assert out["ok"] is False
        assert "went wrong" in out["content"]
    finally:
        await client.close()


async def test_unknown_tool_raises_mcp_error_with_the_server_code():
    client = await connected()
    try:
        with pytest.raises(McpError) as caught:
            await client.call_tool("nope", {}, timeout=30)
        assert caught.value.code == -32602
    finally:
        await client.close()


async def test_pagination_follows_next_cursor():
    client = await connected("paged")
    try:
        tools = await client.list_tools(timeout=30)
        assert [t["name"] for t in tools] == ["fake_read", "fake_echo"]
    finally:
        await client.close()


async def test_ping():
    client = await connected()
    try:
        await client.ping(timeout=30)
    finally:
        await client.close()


async def test_server_that_exits_gives_a_clear_error_with_its_stderr():
    client = McpClient(stdio("exit"), name="fake")
    with pytest.raises(McpError) as caught:
        await client.initialize(timeout=30)
    assert "exited" in caught.value.message
    assert "no credentials" in caught.value.message
    await client.close()


async def test_missing_command_is_an_mcp_error_not_a_crash():
    client = McpClient(StdioTransport("loom-no-such-binary-xyz", []), name="fake")
    with pytest.raises(McpError):
        await client.initialize(timeout=10)
    await client.close()


async def test_request_timeout():
    client = McpClient(stdio("silent"), name="fake")
    with pytest.raises(TimeoutError):
        await client.initialize(timeout=0.4)
    await client.close()


async def test_close_terminates_the_child():
    transport = stdio()
    client = McpClient(transport, name="fake")
    await client.initialize(timeout=30)
    process = transport.process
    assert process is not None
    await client.close()
    assert process.returncode is not None


# ---------------------------------------------------------------------------------
# streamable HTTP
# ---------------------------------------------------------------------------------
def http_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


async def test_http_json_response_and_session_header():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"body": body, "headers": dict(request.headers)})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "h"}}},
            headers={"Mcp-Session-Id": "sess-1", "Content-Type": "application/json"},
        )

    transport = HttpTransport("http://server.test/mcp", {"Authorization": "Bearer t"}, client=http_client(handler))
    client = McpClient(transport, name="http")
    await client.initialize(timeout=5)
    assert client.server_info["name"] == "h"
    assert transport.session_id == "sess-1"
    assert seen[0]["headers"]["accept"] == HttpTransport.ACCEPT
    assert seen[0]["headers"]["authorization"] == "Bearer t"
    assert seen[-1]["headers"]["mcp-session-id"] == "sess-1"  # echoed on the next request
    await client.close()


async def test_http_sse_stream_yields_notifications_then_the_response():
    notifications: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        stream = (
            "event: message\n"
            'data: {"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n'
            "\n"
            "data: " + json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "a"}]}}) + "\n"
            "\n"
        )
        return httpx.Response(200, content=stream, headers={"Content-Type": "text/event-stream"})

    transport = HttpTransport("http://server.test/mcp", client=http_client(handler))
    transport.on_notification(lambda method, params: notifications.append(method))
    result = await transport.request("tools/list", {}, timeout=5)
    assert result == {"tools": [{"name": "a"}]}
    assert notifications == ["notifications/tools/list_changed"]
    await transport.close()


async def test_http_error_response_becomes_mcp_error():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32000, "message": "nope"}})

    transport = HttpTransport("http://server.test/mcp", client=http_client(handler))
    with pytest.raises(McpError) as caught:
        await transport.request("tools/list", {}, timeout=5)
    assert caught.value.message == "nope"
    await transport.close()


async def test_http_status_error_becomes_mcp_error():
    transport = HttpTransport("http://server.test/mcp", client=http_client(lambda r: httpx.Response(503, text="down")))
    with pytest.raises(McpError):
        await transport.request("ping", {}, timeout=5)
    await transport.close()


# ---------------------------------------------------------------------------------
# content flattening
# ---------------------------------------------------------------------------------
def test_flatten_content_shapes():
    assert flatten_content([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert flatten_content([{"type": "resource", "resource": {"uri": "file:///x", "text": "body"}}]) == "[resource file:///x]\nbody"
    assert "[audio audio/wav, 5 bytes]" == flatten_content([{"type": "audio", "mimeType": "audio/wav", "data": "aGVsbG8="}])
    assert flatten_content(None) == ""
