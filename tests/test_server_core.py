"""Surface API core: auth, health, settings, providers/presets/roles, first-run setup.

Everything runs in-process over ASGI; no socket is opened and no model is called."""

from __future__ import annotations

import pytest

from loom import db


# ---------------------------------------------------------------------------------
# health and auth (03 §21.1)
# ---------------------------------------------------------------------------------
async def test_health_is_open_and_reports_state(client):
    response = await client.get("/api/system/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["model_configured"] is True
    assert body["ws_clients"] == 0
    assert set(body) == {"ok", "version", "uptime_s", "first_run", "model_configured", "ws_clients"}


async def test_token_is_enforced_everywhere_but_health(client):
    app = client.loom
    app.settings.set(app.db, "general.lan_token", "s3cret")

    assert (await client.get("/api/system/health")).status_code == 200
    assert (await client.get("/api/settings")).status_code == 401
    assert (await client.get("/api/settings", headers={"Authorization": "Bearer wrong"})).status_code == 401
    ok = await client.get("/api/settings", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


async def test_version_and_queue(client):
    version = (await client.get("/api/system/version")).json()
    assert version["version"] == client.loom.version and version["schema_version"] >= 1
    assert (await client.get("/api/system/queue")).json() == {"depth": 0, "running": []}


async def test_logs_endpoint_returns_the_ring(client):
    import logging

    from loom import log as loom_log

    loom_log.setup(client.loom.home, "DEBUG")
    logging.getLogger("loom.test").warning("something happened")
    records = (await client.get("/api/system/logs", params={"level": "WARNING"})).json()["records"]
    assert any(r["message"] == "something happened" and r["level"] == "WARNING" for r in records)
    assert all(r["level"] in ("WARNING", "ERROR") for r in records)


async def test_reindex_without_the_knowledge_package_is_503(client):
    client.loom.index = None
    assert (await client.post("/api/system/reindex")).status_code == 503


# ---------------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------------
async def test_settings_get_returns_values_schema_and_groups(client):
    body = (await client.get("/api/settings")).json()
    assert body["values"]["conversation.step_budget_desk"] == 25
    assert any(f["key"] == "general.timezone" for f in body["schema"])
    assert body["groups"]["models"] == "Models"


async def test_settings_patch_validates_all_or_nothing_and_emits(client):
    app = client.loom
    seen: list[dict] = []
    app.bus.listen(lambda e: seen.append(e) if e["type"] == "settings.changed" else None)

    response = await client.patch("/api/settings", json={"conversation.step_budget_desk": 40})
    assert response.status_code == 200
    assert response.json()["values"]["conversation.step_budget_desk"] == 40
    assert seen[-1]["data"]["keys"] == ["conversation.step_budget_desk"]

    bad = await client.patch(
        "/api/settings", json={"conversation.step_budget_thread": 99, "conversation.compact_threshold": 5}
    )
    assert bad.status_code == 400 and "compact_threshold" in bad.json()["detail"]
    # nothing was written: the valid key in the same patch did not land either
    assert app.settings.get("conversation.step_budget_thread") == 60

    unknown = await client.patch("/api/settings", json={"nope.nope": 1})
    assert unknown.status_code == 400 and "nope.nope" in unknown.json()["detail"]

    assert (await client.patch("/api/settings", json={})).status_code == 400


async def test_log_level_setting_takes_effect_live(client):
    import logging

    from loom import log as loom_log

    loom_log.setup(client.loom.home, "INFO")
    await client.patch("/api/settings", json={"system.log_level": "ERROR"})
    assert logging.getLogger().level == logging.ERROR


# ---------------------------------------------------------------------------------
# providers / presets / roles
# ---------------------------------------------------------------------------------
async def test_provider_crud_and_delete_is_blocked_while_referenced(client):
    created = await client.post(
        "/api/providers",
        json={"name": "hosted", "kind": "openai_compatible", "base_url": "https://api.example.test/", "params": {"concurrency": 4}},
    )
    assert created.status_code == 201
    assert created.json()["base_url"] == "https://api.example.test"
    assert created.json()["effective_params"]["concurrency"] == 4

    assert (await client.post("/api/providers", json={"name": "hosted", "kind": "ollama", "base_url": "x"})).status_code == 409
    assert (await client.post("/api/providers", json={"name": "x", "kind": "weird", "base_url": "x"})).status_code == 400

    patched = await client.patch("/api/providers/hosted", json={"params": {"timeout_s": 30}})
    assert patched.json()["params"] == {"timeout_s": 30}

    assert (await client.delete("/api/providers/hosted")).status_code == 200
    blocked = await client.delete("/api/providers/local-ollama")
    assert blocked.status_code == 409 and "fake" in blocked.json()["detail"]
    assert (await client.get("/api/providers/nope")).status_code == 404


async def test_preset_crud_usage_and_delete_conflict(client):
    listing = (await client.get("/api/presets")).json()
    fake = next(p for p in listing if p["name"] == "fake")
    assert fake["usage"]["roles"] == ["desk", "worker", "maintenance"] or set(fake["usage"]["roles"]) == {
        "desk",
        "worker",
        "maintenance",
    }
    assert fake["token_factor_seed"] is None

    created = await client.post(
        "/api/presets",
        json={"name": "spare", "provider": "local-ollama", "model": "m", "params": {"max_tokens": 128}},
    )
    assert created.status_code == 201 and created.json()["usage"]["roles"] == []
    assert (await client.post("/api/presets", json={"name": "x", "provider": "gone", "model": "m"})).status_code == 400

    conflict = await client.delete("/api/presets/fake")
    assert conflict.status_code == 409 and "roles" in conflict.json()["detail"]
    assert (await client.delete("/api/presets/spare")).status_code == 200


async def test_preset_patch_keeps_the_machine_written_token_factor_seed(client):
    from loom.models import roles as model_roles

    model_roles.update_token_factor_seed(client.loom, "fake", 1.5)
    patched = await client.patch("/api/presets/fake", json={"params": {"max_tokens": 512}})
    assert patched.json()["token_factor_seed"] == 1.5
    assert patched.json()["params"]["max_tokens"] == 512


async def test_roles_listing_and_assignment(client):
    rows = (await client.get("/api/model-roles")).json()
    assert [r["role"] for r in rows] == ["desk", "worker", "maintenance", "utility", "embedding"]

    seen: list[dict] = []
    client.loom.bus.listen(lambda e: seen.append(e) if e["type"] == "models.changed" else None)

    assert (await client.put("/api/model-roles/utility", json={"preset": "fake"})).json()["preset"] == "fake"
    assert seen[-1]["data"] == {"kind": "role", "name": "utility"}
    assert (await client.put("/api/model-roles/utility", json={"preset": None})).json()["preset"] is None
    assert (await client.put("/api/model-roles/desk", json={"preset": "ghost"})).status_code == 400
    assert (await client.put("/api/model-roles/nope", json={"preset": "fake"})).status_code == 404

    client.loom.db.execute("UPDATE presets SET enabled=0 WHERE name='fake-embed'")
    disabled = await client.put("/api/model-roles/embedding", json={"preset": "fake-embed"})
    assert disabled.status_code == 400 and "disabled" in disabled.json()["detail"]


async def test_probe_and_models_use_the_client(client):
    probe = (await client.post("/api/providers/local-ollama/probe")).json()
    assert probe["ok"] is True and "fake-model" in probe["models"]
    listing = (await client.get("/api/models", params={"provider": "local-ollama"})).json()
    assert listing["providers"][0]["models"] == ["fake-model", "nomic-embed-text"]
    assert (await client.get("/api/models", params={"provider": "ghost"})).status_code == 404


async def test_preset_test_endpoint(client, fake):
    from loom.models.fake import Scripted

    fake.script(Scripted(content="OK"))
    body = (await client.post("/api/presets/fake/test")).json()
    assert body["outcome"] == "ok" and body["content"] == "OK"
    assert (await client.post("/api/presets/ghost/test")).status_code == 404


# ---------------------------------------------------------------------------------
# first run (03 §3.3)
# ---------------------------------------------------------------------------------
async def test_setup_state_reports_detected_providers(client):
    body = (await client.get("/api/system/setup")).json()
    assert body["needed"] is False  # the fixture already configured a desk role
    assert body["providers_detected"][0]["name"] == "local-ollama"
    assert "nomic-embed-text" in body["providers_detected"][0]["models"]
    assert body["embedding_available"] is True
    assert body["projects_root"]


async def test_setup_creates_preset_roles_settings_and_the_first_item(client):
    app = client.loom
    app.db.execute("UPDATE model_roles SET preset=NULL")
    app.db.execute("DELETE FROM presets")
    events: list[dict] = []
    app.bus.listen(lambda e: events.append(e))

    response = await client.post(
        "/api/system/setup",
        json={
            "provider": "local-ollama",
            "model": "qwen3.6:35b",
            "embedding_model": "nomic-embed-text:latest",
            "timezone": "America/Denver",
            "projects_root": "C:/Work",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["preset"] == "qwen3.6-35b"
    assert body["roles"]["desk"] == body["roles"]["worker"] == body["roles"]["maintenance"] == "qwen3.6-35b"
    assert body["roles"]["embedding"] == "nomic-embed-text"

    preset = app.db.execute("SELECT * FROM presets WHERE name='qwen3.6-35b'").fetchone()
    params = db.loads(preset["params"], {})
    assert params["max_tokens"] == 4096 and params["num_ctx"] == 32768 and params["reasoning"] == "on"
    assert app.settings.get("general.timezone") == "America/Denver"
    assert app.settings.get("general.projects_root") == "C:/Work"
    assert app.has_model("desk")

    kinds = [e["type"] for e in events]
    assert "item.created" in kinds and "settings.changed" in kinds and "models.changed" in kinds
    item = app.db.execute("SELECT title, body FROM items ORDER BY created_at DESC LIMIT 1").fetchone()
    assert item["title"] == "Welcome" and "Tell me about yourself" in item["body"]

    assert (await client.get("/api/system/setup")).json()["needed"] is False


async def test_setup_rejects_an_unknown_provider_and_a_missing_secrets_store(client):
    assert (await client.post("/api/system/setup", json={"provider": "ghost", "model": "m"})).status_code == 404
    client.loom.secrets = None
    blocked = await client.post(
        "/api/system/setup",
        json={"model": "m", "openrouter": {"base_url": "https://openrouter.ai/api", "api_key": "sk-x"}},
    )
    assert blocked.status_code == 503 and "secrets" in blocked.json()["detail"]


async def test_setup_stores_an_openrouter_key_when_secrets_exist(client):
    stored: dict[str, str] = {}

    class Secrets:
        def set(self, name: str, value: str) -> None:
            stored[name] = value

        def get(self, name: str) -> str:
            return stored.get(name, "")

    client.loom.secrets = Secrets()
    response = await client.post(
        "/api/system/setup",
        json={
            "model": "anthropic/claude",
            "openrouter": {"base_url": "https://openrouter.ai/api", "api_key": "sk-or-x"},
        },
    )
    assert response.status_code == 200 and response.json()["provider"] == "openrouter"
    assert stored["openrouter_api_key"] == "sk-or-x"
    row = client.loom.db.execute("SELECT kind, api_key_ref FROM providers WHERE name='openrouter'").fetchone()
    assert row["kind"] == "openai_compatible" and row["api_key_ref"] == "openrouter_api_key"


# ---------------------------------------------------------------------------------
# websocket + static UI
# ---------------------------------------------------------------------------------
async def test_websocket_forwards_bus_events_and_counts_clients(app):
    from fastapi.testclient import TestClient

    from loom.server.app import create_app

    api = create_app(app)
    with TestClient(api) as http:
        with http.websocket_connect("/ws") as socket:
            socket.send_json({"subscribe": {"conversations": "all"}})
            assert socket.receive_json()["type"] == "subscribed"
            socket.send_json({"type": "ping"})
            assert socket.receive_json()["type"] == "pong"
            app.bus.emit("item.created", {"id": "x"}, "c1")
            event = socket.receive_json()
            assert event["type"] == "item.created"
            assert event["conversation_id"] == "c1"
            assert event["data"] == {"id": "x"}
            assert app.extras["hub"].connected() == 1
    assert app.extras["hub"].connected() == 0


async def test_websocket_requires_the_token_when_set(app):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from loom.server.app import create_app

    app.settings.set(app.db, "general.lan_token", "s3cret")
    api = create_app(app)
    with TestClient(api) as http:
        with pytest.raises(WebSocketDisconnect):
            with http.websocket_connect("/ws") as socket:
                socket.receive_json()
        with http.websocket_connect("/ws?token=s3cret") as socket:
            socket.send_json({"type": "ping"})
            assert socket.receive_json()["type"] == "pong"


async def test_root_serves_the_ui_with_an_spa_fallback(client):
    root = await client.get("/")
    assert root.status_code == 200 and "<html" in root.text.lower()
    deep = await client.get("/threads/01J8")  # a client-side route, not a file
    assert deep.status_code == 200 and deep.text == root.text
    assert (await client.get("/api/nothing-here")).status_code == 404


async def test_an_unbuilt_ui_explains_itself_instead_of_404ing(app, monkeypatch):
    import httpx

    from loom.server import app as server_app

    monkeypatch.setattr(server_app, "ui_dist", lambda: None)
    api = server_app.create_app(app)
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://loom.test") as http:
        response = await http.get("/")
        assert response.status_code == 200
        assert "npm --prefix ui run build" in response.text
        assert (await http.get("/api/nothing-here")).status_code == 404


# ---------------------------------------------------------------------------------
# queued input
# ---------------------------------------------------------------------------------
async def test_inbox_endpoint_lists_rows_not_yet_in_the_transcript(client):
    app = client.loom
    desk = app.desk_id()
    app.db.execute(
        "INSERT INTO inbox(conversation_id, source, content, meta, created_at) VALUES (?, 'user', 'later', '{}', ?)",
        (desk, db.now_iso()),
    )
    rows = (await client.get(f"/api/conversations/{desk}/inbox")).json()
    assert [r["content"] for r in rows] == ["later"]
    assert set(rows[0]) == {"id", "conversation_id", "source", "content", "meta", "created_at"}
    assert (await client.get("/api/conversations/nope/inbox")).status_code == 404


async def test_events_tail_returns_the_newest_page_ascending(client):
    from loom.conversation import events as ev

    app = client.loom
    desk = app.desk_id()
    for i in range(5):
        ev.append(app, desk, "user", f"m{i}")
    oldest = (await client.get(f"/api/conversations/{desk}/events", params={"limit": 2})).json()
    newest = (await client.get(f"/api/conversations/{desk}/events", params={"limit": 2, "tail": "true"})).json()
    assert [e["content"] for e in oldest] == ["m0", "m1"]
    assert [e["content"] for e in newest] == ["m3", "m4"]
    before = (await client.get(f"/api/conversations/{desk}/events", params={"limit": 2, "before": newest[0]["id"]})).json()
    assert [e["content"] for e in before] == ["m1", "m2"]
