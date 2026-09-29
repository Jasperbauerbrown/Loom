"""Surface API core: auth, health, settings, providers/presets/roles, the first-run item.

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


async def test_preset_rename_moves_every_reference_with_it(client):
    app = client.loom
    app.db.execute("UPDATE conversations SET preset='fake'")
    pointing = app.db.execute("SELECT COUNT(*) FROM conversations WHERE preset='fake'").fetchone()[0]
    assert pointing >= 1, "the fixture should have at least the desk to repoint"
    created_at = app.db.execute("SELECT created_at FROM presets WHERE name='fake'").fetchone()[0]
    seen: list[dict] = []
    app.bus.listen(lambda e: seen.append(e) if e["type"] == "models.changed" else None)

    # A taken name is refused before anything moves; the same name is a plain patch.
    assert (await client.patch("/api/presets/fake", json={"name": "fake-embed"})).status_code == 409
    assert (await client.patch("/api/presets/fake", json={"name": "fake", "notes": "same"})).json()["notes"] == "same"
    assert "renamed_from" not in seen[-1]["data"]

    renamed = await client.patch("/api/presets/fake", json={"name": "bar", "params": {"max_tokens": 64}})
    assert renamed.status_code == 200
    body = renamed.json()
    assert body["name"] == "bar" and body["params"]["max_tokens"] == 64 and body["created_at"] == created_at
    assert set(body["usage"]["roles"]) == {"desk", "worker", "maintenance"}
    assert seen[-1]["data"] == {"kind": "preset", "name": "bar", "renamed_from": "fake"}

    assert (await client.get("/api/presets/fake")).status_code == 404
    roles = {r["role"]: r["preset"] for r in (await client.get("/api/model-roles")).json()}
    assert roles["desk"] == roles["worker"] == roles["maintenance"] == "bar"
    assert app.db.execute("SELECT COUNT(*) FROM conversations WHERE preset='bar'").fetchone()[0] == pointing
    assert app.db.execute("SELECT COUNT(*) FROM conversations WHERE preset='fake'").fetchone()[0] == 0
    assert app.db.execute("PRAGMA foreign_key_check").fetchall() == []
    # Role resolution follows the new name without a restart.
    from loom.models import roles as model_roles

    assert model_roles.resolve(app, role="desk").preset["name"] == "bar"


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
# first run (03 §3.3): no screen, one now-board item
# ---------------------------------------------------------------------------------
async def test_an_unconfigured_desk_role_posts_one_setup_item_however_often_we_start(app):
    """The Setup screen is gone (2026-09-22); a missing model is said on the now board
    and never blocks. Restarting must not stack the item up."""
    from loom.server.app import _post_choose_a_model

    app.db.execute("UPDATE model_roles SET preset=NULL")
    assert not app.has_model("desk")

    _post_choose_a_model(app)
    _post_choose_a_model(app)
    rows = app.db.execute("SELECT title, body FROM items WHERE kind='setup' AND source='setup'").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "Choose a model" and "Settings" in rows[0]["body"]


async def test_the_setup_item_returns_once_the_person_dismisses_it_and_still_has_no_model(app):
    from loom import items
    from loom.server.app import _post_choose_a_model

    app.db.execute("UPDATE model_roles SET preset=NULL")
    _post_choose_a_model(app)
    row = app.db.execute("SELECT id FROM items WHERE kind='setup' AND source='setup'").fetchone()
    items.dismiss(app, row["id"])

    _post_choose_a_model(app)  # dismissed: the person answered, but the model is still unset
    assert app.db.execute(
        "SELECT COUNT(*) FROM items WHERE kind='setup' AND source='setup'"
    ).fetchone()[0] == 2


async def test_configuring_the_desk_role_dismisses_the_choose_a_model_item(client):
    """The writer that posts "Choose a model" checks the same predicate to answer it
    (03 §11.3 item lifecycle): once a desk preset is set, `has_model("desk")` holds and
    the item is gone."""
    app = client.loom
    from loom.server.app import _post_choose_a_model

    app.db.execute("UPDATE model_roles SET preset=NULL WHERE role='desk'")
    _post_choose_a_model(app)
    row = app.db.execute("SELECT id, dismissed_at FROM items WHERE kind='setup' AND source='setup'").fetchone()
    assert row is not None and row["dismissed_at"] is None

    response = await client.put("/api/model-roles/desk", json={"preset": "fake"})
    assert response.status_code == 200

    after = app.db.execute("SELECT dismissed_at FROM items WHERE id=?", (row["id"],)).fetchone()
    assert after["dismissed_at"] is not None


async def test_the_setup_endpoints_are_gone(client):
    # GET falls to the SPA catch-all, which 404s anything under /api/; because that
    # catch-all owns GET on every path, the vanished POST reads as 405.
    assert (await client.get("/api/system/setup")).status_code == 404
    assert (await client.post("/api/system/setup", json={"model": "m"})).status_code == 405


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


async def test_usage_endpoint_sums_meta_and_groups_by_turn(client):
    from loom.conversation import events as ev

    app = client.loom
    desk = app.desk_id()
    u1 = ev.append(app, desk, "user", "hi")
    ev.append(app, desk, "assistant", "hello", {"usage": {"prompt_tokens": 10, "completion_tokens": 4}})
    u2 = ev.append(app, desk, "user", "again")
    ev.append(app, desk, "tool_call", "[]", {"usage": {"prompt_tokens": 20, "completion_tokens": 5}})
    ev.append(app, desk, "tool_result", "ok", {"call_id": "x", "ok": True})
    ev.append(app, desk, "assistant", "done", {"usage": {"prompt_tokens": 8}})  # completion not reported

    body = (await client.get(f"/api/conversations/{desk}/usage")).json()
    assert body["prompt_tokens"] == 38
    assert body["completion_tokens"] == 9
    assert body["steps"] == 3
    assert body["steps_with_usage"] == 3
    assert len(body["turns"]) == 2
    t1, t2 = body["turns"]
    assert t1["user_event_id"] == u1 and (t1["prompt_tokens"], t1["completion_tokens"], t1["steps"]) == (10, 4, 1)
    assert t2["user_event_id"] == u2 and (t2["prompt_tokens"], t2["completion_tokens"], t2["steps"]) == (28, 5, 2)
    assert (await client.get("/api/conversations/nope/usage")).status_code == 404


# ---------------------------------------------------------------------------------
# conversations graph (the Graph screen's conversations layer, 03 §13.1)
# ---------------------------------------------------------------------------------
async def test_conversations_graph_has_spawn_pinned_and_walkthrough_edges(client):
    from loom.conversation import events as ev

    app = client.loom
    desk = app.desk_id()
    app.vault.write("projects/x.md", "Project x.", op="create", frontmatter={"type": "project"})
    r = await client.post(
        "/api/conversations", json={"goal": "build x", "notes": ["projects/x.md"], "parent_id": desk}
    )
    assert r.status_code == 201
    tid = r.json()["id"]
    ev.update(app, tid, walkthrough="daily/2026-09-29.md")

    body = (await client.get("/api/conversations/graph")).json()
    assert set(body) == {"nodes", "edges"}
    ids = {n["id"] for n in body["nodes"]}
    assert {desk, tid} <= ids
    thread_node = next(n for n in body["nodes"] if n["id"] == tid)
    assert set(thread_node) == {"id", "kind", "title", "goal", "status", "parent_id"}
    assert thread_node["parent_id"] == desk

    assert {"src": desk, "dst": tid, "kind": "spawned"} in body["edges"]
    assert {"src": tid, "dst": "projects/x.md", "kind": "pinned"} in body["edges"]
    assert {"src": tid, "dst": "daily/2026-09-29.md", "kind": "walkthrough"} in body["edges"]

    # "graph" is not swallowed by /conversations/{id}
    assert (await client.get(f"/api/conversations/{tid}")).status_code == 200
