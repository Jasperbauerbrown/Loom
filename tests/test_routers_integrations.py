"""`/api/mcp-servers` and `/api/connectors` (03 §12.1, §13.2 Integrations).

The `client` fixture runs the real lifespan, so these go through the same install/start
path the server does.
"""

from __future__ import annotations

import sys
from pathlib import Path

SERVER = str(Path(__file__).parent / "fake_mcp_server.py")


def body(name="fake", mode="normal", enabled=False):
    return {
        "name": name,
        "transport": "stdio",
        "command": sys.executable,
        "args": [SERVER, mode],
        "env": {},
        "enabled": enabled,
    }


# ---------------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------------
async def test_mcp_server_crud(client):
    assert (await client.get("/api/mcp-servers")).json() == []

    created = await client.post("/api/mcp-servers", json=body())
    assert created.status_code == 201
    row = created.json()
    assert row["name"] == "fake" and row["args"][1] == "normal" and row["enabled"] is False
    server_id = row["id"]

    got = await client.get(f"/api/mcp-servers/{server_id}")
    assert got.json()["id"] == server_id

    patched = await client.patch(f"/api/mcp-servers/{server_id}", json={"env": {"A": "1"}, "args": [SERVER, "paged"]})
    assert patched.status_code == 200
    assert patched.json()["env"] == {"A": "1"}
    assert patched.json()["args"][1] == "paged"

    listed = (await client.get("/api/mcp-servers")).json()
    assert [r["name"] for r in listed] == ["fake"]

    deleted = await client.delete(f"/api/mcp-servers/{server_id}")
    assert deleted.json() == {"deleted": server_id}
    assert (await client.get(f"/api/mcp-servers/{server_id}")).status_code == 404


async def test_mcp_server_validation_errors_are_detail_400(client):
    bad = await client.post("/api/mcp-servers", json={**body(), "command": ""})
    assert bad.status_code == 400 and "command" in bad.json()["detail"]

    await client.post("/api/mcp-servers", json=body(name="dupe"))
    again = await client.post("/api/mcp-servers", json=body(name="dupe"))
    assert again.status_code == 400 and "already exists" in again.json()["detail"]


async def test_mcp_test_refresh_and_reload(client):
    server_id = (await client.post("/api/mcp-servers", json=body())).json()["id"]

    tested = (await client.post(f"/api/mcp-servers/{server_id}/test")).json()
    assert tested["ok"] is True and tested["tools"] == 2

    reloaded = (await client.post(f"/api/mcp-servers/{server_id}/reload")).json()
    assert reloaded["status"] == "disconnected"  # the row is disabled; reload respects it

    enabled = (await client.patch(f"/api/mcp-servers/{server_id}", json={"enabled": True})).json()
    assert enabled["status"] == "connected"
    assert [t["name"] for t in enabled["tools_cache"]] == ["fake_read", "fake_echo"]

    refreshed = (await client.post(f"/api/mcp-servers/{server_id}/refresh")).json()
    assert refreshed["status"] == "connected"

    await client.delete(f"/api/mcp-servers/{server_id}")


async def test_mcp_test_reports_a_failure_without_raising(client):
    server_id = (await client.post("/api/mcp-servers", json=body(mode="exit"))).json()["id"]
    tested = (await client.post(f"/api/mcp-servers/{server_id}/test")).json()
    assert tested["ok"] is False and "exited" in tested["error"]


async def test_missing_server_is_404(client):
    assert (await client.get("/api/mcp-servers/nope")).status_code == 404
    assert (await client.post("/api/mcp-servers/nope/refresh")).status_code == 404
    assert (await client.patch("/api/mcp-servers/nope", json={"enabled": False})).status_code == 404
    assert (await client.delete("/api/mcp-servers/nope")).status_code == 404


# ---------------------------------------------------------------------------------
# connectors
# ---------------------------------------------------------------------------------
async def test_connectors_list_carries_schema_and_tools(client):
    rows = (await client.get("/api/connectors")).json()
    echo = next(r for r in rows if r["name"] == "example_echo")
    assert echo["status"] == "loaded"
    assert echo["tools"] == ["example_echo_echo"]
    assert echo["facets"] == ["tools", "events"]
    assert "interval_s" in echo["config_schema"]["properties"]
    assert echo["description"]


async def test_connector_patch_config_and_enabled(client):
    row = (await client.patch("/api/connectors/example_echo", json={"config": {"interval_s": 120}})).json()
    assert row["config"] == {"interval_s": 120, "emit_events": False}
    assert row["status"] == "loaded"

    row = (await client.patch("/api/connectors/example_echo", json={"enabled": False})).json()
    assert row["status"] == "disabled"
    assert client.loom.tools.get("example_echo_echo") is None

    row = (await client.patch("/api/connectors/example_echo", json={"enabled": True})).json()
    assert row["status"] == "loaded"


async def test_connector_bad_config_is_400(client):
    bad = await client.patch("/api/connectors/example_echo", json={"config": {"interval_s": "soon"}})
    assert bad.status_code == 400
    assert "interval_s" in bad.json()["detail"]


async def test_connector_reload_and_404(client):
    reloaded = (await client.post("/api/connectors/example_echo/reload")).json()
    assert reloaded["status"] == "loaded"
    assert (await client.get("/api/connectors/nope")).status_code == 404
    assert (await client.post("/api/connectors/nope/reload")).status_code == 404
    assert (await client.patch("/api/connectors/nope", json={"enabled": True})).status_code == 404


async def test_mutations_emit_their_ws_event(client):
    app = client.loom
    seen: list[dict] = []
    app.bus.listen(lambda event: seen.append(event))
    try:
        await client.post("/api/mcp-servers", json=body(name="evented"))
        await client.patch("/api/connectors/example_echo", json={"enabled": False})
        types = [e["type"] for e in seen]
        assert "mcp.status" in types
        assert "connector.status" in types
    finally:
        app.bus._listeners.clear()
        await client.patch("/api/connectors/example_echo", json={"enabled": True})
