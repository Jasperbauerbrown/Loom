"""`GET /api/maintenance` and `POST /api/maintenance/run` (03 §12.1)."""

from __future__ import annotations

import asyncio

from loom.conversation import events
from loom.models.fake import Scripted


async def test_status_reports_the_schedule_and_no_report_yet(client):
    response = await client.get("/api/maintenance")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["running"] is False
    assert body["last_report"] is None
    assert body["idle_seconds"] == 120
    assert body["next_nightly_at"].startswith("20")


async def test_run_starts_a_pass_and_the_report_appears(client):
    app = client.loom
    app.models.on("maintenance", lambda _r: Scripted(content="nothing to record"))
    events.append(app, app.desk_id(), "user", "The NAS is at 192.168.1.9.")

    response = await client.post("/api/maintenance/run")
    assert response.status_code == 202
    assert response.json()["started"] is True

    for _ in range(200):
        if app.maintenance.report is not None:
            break
        await asyncio.sleep(0.01)

    body = (await client.get("/api/maintenance")).json()
    assert body["last_report"]["reason"] == "manual"
    assert body["last_report"]["consolidated"][0]["events"] == 1
    assert body["running"] is False


async def test_run_is_refused_while_a_pass_is_in_flight(client):
    app = client.loom
    await app.maintenance.lock.acquire()
    try:
        response = await client.post("/api/maintenance/run")
        assert response.status_code == 409
        assert (await client.get("/api/maintenance")).json()["running"] is True
    finally:
        app.maintenance.lock.release()
