"""Apply on a maintenance hand-back (03 §8.6): the stored call runs through the one
dispatcher with the click as the approval; the outcome lands on the item."""

from __future__ import annotations

import asyncio

from loom import items
from loom.conversation import events
from loom.knowledge import maintenance as M
from loom.models.fake import Scripted
from loom.tools import approvals

APPEND = {"path": "core/preferences.md", "op": "append", "content": "- Metric units."}


def hand_back(app, args: dict) -> dict:
    return items.post(
        app, "knowledge", f"Maintenance wants to write {args['path']}", source=M.SOURCE,
        action={"kind": "knowledge_write", "args": args},
    )


async def test_apply_runs_the_gated_write_and_dismisses_the_item(client):
    app = client.loom
    item = hand_back(app, APPEND)

    r = await client.post(f"/api/items/{item['id']}/apply")

    assert r.status_code == 200
    row = r.json()
    assert row["dismissed_at"]
    assert row["action"]["result"]["state"] == "ok"
    assert "- Metric units." in app.vault.read("core/preferences.md").body
    # The gate asked (core/ is `ask`) and the click answered it once: no pending row,
    # nothing widened, the desk is not left waiting.
    assert approvals.pending(app) == []
    allowed = approvals.listed(app, "allowed")
    assert [a["decision"] for a in allowed] == ["allow"]
    assert app.conversation(app.desk_id())["status"] != "waiting_approval"
    # The click answered before anything was surfaced: no "Approval needed" item at all.
    every = items.list_items(app, include_dismissed=True)
    assert not [i for i in every if i["source"].startswith("approval:")]


async def test_apply_spends_no_judge_call_on_a_classify_policy(app, monkeypatch):
    """A click outranks the judge: under `classify` the gate asks and the click answers."""
    from loom.tools import classify, permissions

    async def judge(*_a, **_k):
        raise AssertionError("the judge must not be called for an Apply")

    monkeypatch.setattr(classify, "judge", judge)
    permissions.set_policy(app, "knowledge_write", "classify")
    item = hand_back(app, {"path": "notes/nas.md", "op": "create", "content": "The NAS."})

    row = await M.apply_hand_back(app, item["id"])

    assert row["action"]["result"]["state"] == "ok"
    assert [a["decision"] for a in approvals.listed(app, "allowed")] == ["allow"]
    assert "The NAS." in app.vault.read("notes/nas.md").body


async def test_a_failed_apply_keeps_the_item_and_reports_the_error(client):
    app = client.loom
    item = hand_back(app, {"path": "core/preferences.md", "op": "replace", "old": "text that is not there", "new": "x"})

    r = await client.post(f"/api/items/{item['id']}/apply")

    assert r.status_code == 200
    row = r.json()
    assert row["dismissed_at"] is None
    assert row["action"]["result"]["state"] == "error"
    assert row["action"]["result"]["content"]
    assert row["action"]["args"]["old"] == "text that is not there"  # the call is kept for a retry


async def test_a_double_click_writes_once(app):
    item = hand_back(app, APPEND)

    first, second = await asyncio.gather(M.apply_hand_back(app, item["id"]), M.apply_hand_back(app, item["id"]))
    again = await M.apply_hand_back(app, item["id"])

    assert app.vault.read("core/preferences.md").body.count("- Metric units.") == 1
    assert len(approvals.listed(app, "allowed")) == 1
    assert again["dismissed_at"] and again["action"]["result"]["state"] == "ok"


async def test_an_allowed_call_needs_no_approval(app):
    item = hand_back(app, {"path": "notes/nas.md", "op": "create", "content": "The NAS."})

    row = await M.apply_hand_back(app, item["id"])

    assert row["action"]["result"]["state"] == "ok"
    assert approvals.listed(app) == []
    assert "The NAS." in app.vault.read("notes/nas.md").body


async def test_items_without_a_call_are_refused(client):
    plain = items.post(client.loom, "knowledge", "Maintenance: 2 writes")

    assert (await client.post(f"/api/items/{plain['id']}/apply")).status_code == 409
    assert (await client.post("/api/items/nope/apply")).status_code == 404


async def test_a_real_hand_back_applies(app, fake):
    """End to end: a pass hands the core write back, the click applies it."""
    cid = app.desk_id()
    events.append(app, cid, "user", "Always use metric units.")
    queue = [Scripted(tool_calls=[("knowledge_write", APPEND)]), Scripted(content="handed over")]
    fake.on("maintenance", lambda _r: queue.pop(0) if queue else Scripted(content="nothing"))
    await app.maintenance.run("test")
    [posted] = [i for i in items.list_items(app) if i["action"].get("kind") == "knowledge_write"]

    row = await M.apply_hand_back(app, posted["id"])

    assert row["dismissed_at"]
    assert "- Metric units." in app.vault.read("core/preferences.md").body
