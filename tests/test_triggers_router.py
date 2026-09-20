"""`/api/schedules` (03 §12.1): the round trip the Settings → Triggers screen makes."""

from __future__ import annotations

import pytest

from loom import items


@pytest.fixture
def body():
    return {"spec": "daily at 09:00", "target": "desk", "payload": "check the build"}


async def test_schedules_crud_round_trip(client, body):
    created = await client.post("/api/schedules", json=body)
    assert created.status_code == 201
    row = created.json()
    assert row["spec"] == "daily at 09:00" and row["enabled"] == 1 and row["next_run_at"]

    listed = await client.get("/api/schedules")
    assert [r["id"] for r in listed.json()] == [row["id"]]
    assert (await client.get(f"/api/schedules/{row['id']}")).json() == row

    patched = await client.patch(f"/api/schedules/{row['id']}", json={"spec": "every 2h", "policy": "interrupt"})
    assert patched.status_code == 200
    assert patched.json()["spec"] == "every 2h" and patched.json()["policy"] == "interrupt"
    assert patched.json()["next_run_at"] != row["next_run_at"]

    assert (await client.delete(f"/api/schedules/{row['id']}")).status_code == 200
    assert (await client.get("/api/schedules")).json() == []


async def test_run_now_fires_the_schedule(client, body, fake):
    from loom.models.fake import Scripted

    fake.script(Scripted(content="ok"))
    row = (await client.post("/api/schedules", json=body)).json()
    ran = await client.post(f"/api/schedules/{row['id']}/run")
    assert ran.status_code == 200 and ran.json()["runs"] == 1
    assert ran.json()["next_run_at"] == row["next_run_at"]  # run-now does not shift it
    board = items.list_items(client.loom)
    assert board[0]["kind"] == "schedule_result" and board[0]["source"] == f"schedule:{row['id']}"


async def test_errors_are_400_404_and_409(client, body):
    bad_spec = await client.post("/api/schedules", json={**body, "spec": "whenever"})
    assert bad_spec.status_code == 400 and "accepted forms" in bad_spec.json()["detail"]

    app = client.loom
    app.settings.set(app.db, "triggers.max_active_schedules", 0)
    capped = await client.post("/api/schedules", json=body)
    assert capped.status_code == 409 and "max_active_schedules" in capped.json()["detail"]
    app.settings.set(app.db, "triggers.max_active_schedules", 25)

    missing = await client.post("/api/schedules", json={**body, "target": "01GONE"})
    assert missing.status_code == 409

    for method, path in (("get", "/api/schedules/nope"), ("patch", "/api/schedules/nope"), ("delete", "/api/schedules/nope"), ("post", "/api/schedules/nope/run")):
        response = await getattr(client, method)(path, **({"json": {"payload": "x"}} if method == "patch" else {}))
        assert response.status_code == 404

    row = (await client.post("/api/schedules", json=body)).json()
    assert (await client.patch(f"/api/schedules/{row['id']}", json={"runs": 9})).status_code == 400
