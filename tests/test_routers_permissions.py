"""The permission, approval and secret endpoints (03 §12.1). Few and shallow: the
behaviour lives in the modules these routers call, so these check wiring, validation,
status codes and the events every mutation must emit."""

from __future__ import annotations

import pytest

from loom.tools import approvals, fs_guard, permissions


@pytest.fixture
def events(app):
    seen: list[dict] = []
    app.bus.listen(seen.append)
    return seen


def _keys(events) -> list[list[str]]:
    return [e["data"]["keys"] for e in events if e["type"] == "settings.changed"]


# ---------------------------------------------------------------------------------
# tool policies
# ---------------------------------------------------------------------------------
async def test_list_tool_policies_covers_the_registry(client):
    response = await client.get("/api/tool-policies")
    assert response.status_code == 200
    rows = {r["tool"]: r for r in response.json()}
    assert rows["shell"]["mode"] == "classify"
    assert rows["fs_read"]["read_only"] is True
    assert rows["knowledge_write:core/"]["kind"] == "path_class"


async def test_put_and_delete_a_tool_policy(client, app, events):
    response = await client.put("/api/tool-policies/shell", json={"mode": "ask"})
    assert response.status_code == 200 and response.json()["mode"] == "ask"
    assert permissions.row_mode(app, "shell") == "ask"
    assert ["tool_policies"] in _keys(events)

    response = await client.delete("/api/tool-policies/shell")
    assert response.status_code == 200
    assert permissions.row_mode(app, "shell") is None


async def test_put_accepts_a_path_class_key(client, app):
    response = await client.put("/api/tool-policies/knowledge_write:projects/", json={"mode": "ask"})
    assert response.status_code == 200
    assert permissions.row_mode(app, "knowledge_write:projects/") == "ask"


async def test_policy_validation(client):
    assert (await client.put("/api/tool-policies/shell", json={"mode": "maybe"})).status_code == 400
    assert (await client.put("/api/tool-policies/nosuchtool", json={"mode": "ask"})).status_code == 404
    assert (await client.delete("/api/tool-policies/nosuchtool")).status_code == 404


# ---------------------------------------------------------------------------------
# roots
# ---------------------------------------------------------------------------------
async def test_roots_crud(client, app, tmp_path, events):
    target = tmp_path / "newroot"
    response = await client.post("/api/roots", json={"path": str(target)})
    assert response.status_code == 200
    stored = response.json()["path"]
    assert response.json()["added_by"] == "user"
    assert ["roots"] in _keys(events)

    listed = (await client.get("/api/roots")).json()
    assert stored in [r["path"] for r in listed]

    assert (await client.delete("/api/roots", params={"path": stored})).status_code == 200
    assert stored not in [r["path"] for r in (await client.get("/api/roots")).json()]
    assert (await client.delete("/api/roots", params={"path": stored})).status_code == 404


async def test_adding_a_root_needs_a_path(client):
    assert (await client.post("/api/roots", json={})).status_code == 400


# ---------------------------------------------------------------------------------
# protected paths
# ---------------------------------------------------------------------------------
async def test_adding_a_protected_path(client, app, tmp_path, events):
    response = await client.post("/api/protected-paths", json={"path": str(tmp_path / "keys")})
    assert response.status_code == 200
    assert fs_guard.is_protected(app, str(tmp_path / "keys")) is True
    assert ["protected_paths"] in _keys(events)


async def test_removing_a_protected_path_is_itself_an_approval(client, app, tmp_path, monkeypatch):
    monkeypatch.setattr("loom.conversation.wake", lambda app, cid: pytest.fail("no turn is waiting"))
    path = fs_guard.add_protected(app, str(tmp_path / "keys"))

    response = await client.delete("/api/protected-paths", params={"path": path})
    assert response.status_code == 202
    approval = response.json()["approval"]
    assert approval["status"] == "pending"
    assert approval["tool"] == approvals.INTERNAL_TOOL
    assert fs_guard.is_protected(app, path) is True  # nothing removed yet
    assert app.conversation(app.desk_id())["status"] == "waiting_approval"

    decided = await client.post(f"/api/approvals/{approval['id']}", json={"decision": "allow"})
    assert decided.status_code == 200
    assert fs_guard.is_protected(app, path) is False
    assert app.conversation(app.desk_id())["status"] == "idle"


async def test_denying_the_removal_keeps_the_protection(client, app, tmp_path):
    path = fs_guard.add_protected(app, str(tmp_path / "keys"))
    approval = (await client.delete("/api/protected-paths", params={"path": path})).json()["approval"]
    await client.post(f"/api/approvals/{approval['id']}", json={"decision": "deny"})
    assert fs_guard.is_protected(app, path) is True


async def test_removing_a_path_that_is_not_protected_is_404(client, tmp_path):
    response = await client.delete("/api/protected-paths", params={"path": str(tmp_path / "unknown")})
    assert response.status_code == 404


# ---------------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------------
async def test_approvals_list_get_and_decide(client, app, tmp_path, monkeypatch):
    monkeypatch.setattr("loom.conversation.wake", lambda app, cid: None)
    path = fs_guard.add_protected(app, str(tmp_path / "keys"))
    approval = (await client.delete("/api/protected-paths", params={"path": path})).json()["approval"]

    pending = (await client.get("/api/approvals", params={"status": "pending"})).json()
    assert [a["id"] for a in pending] == [approval["id"]]
    assert (await client.get(f"/api/approvals/{approval['id']}")).json()["reason"].startswith("remove ")
    assert (await client.get("/api/approvals/NOPE")).status_code == 404

    assert (await client.post(f"/api/approvals/{approval['id']}", json={"decision": "sideways"})).status_code == 400
    assert (await client.post("/api/approvals/NOPE", json={"decision": "allow"})).status_code == 404
    assert (await client.post(f"/api/approvals/{approval['id']}", json={"decision": "allow"})).status_code == 200
    assert (await client.post(f"/api/approvals/{approval['id']}", json={"decision": "allow"})).status_code == 409


# ---------------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------------
async def test_secrets_are_write_only(client, app, events):
    assert (await client.get("/api/secrets")).json() == {
        "names": [],
        "backend": app.secrets.backend,
        "warning": app.secrets.warning,
    }

    response = await client.put("/api/secrets/API_KEY", json={"value": "sup3r-s3cret"})
    assert response.status_code == 200 and response.json()["stored"] is True
    assert "sup3r-s3cret" not in response.text
    assert ["secrets"] in _keys(events)

    listing = (await client.get("/api/secrets")).json()
    assert listing["names"] == ["API_KEY"]
    assert "sup3r-s3cret" not in str(listing)

    assert (await client.delete("/api/secrets/API_KEY")).status_code == 200
    assert (await client.delete("/api/secrets/API_KEY")).status_code == 404
    assert (await client.get("/api/secrets")).json()["names"] == []


async def test_a_secret_needs_a_non_empty_value_and_a_legal_name(client):
    assert (await client.put("/api/secrets/API_KEY", json={"value": ""})).status_code == 400
    assert (await client.put("/api/secrets/bad name", json={"value": "x"})).status_code == 400
