"""A pending approval notifies (TD audit F1, backlog "Pending approval notifies").

`approvals.create` posts exactly one `notification` item on the interrupt path — the
same `items.post(notify=True)` every other surface already renders — naming the
conversation, the tool and the reason. Answering the approval resolves the item.
"""

from __future__ import annotations

import pytest

from loom import db, items
from loom.ids import ulid
from loom.models.types import ToolCall
from loom.tools import approvals, dispatch, permissions
from loom.tools.dispatch import ApprovalNeeded
from loom.tools.registry import Tool, ToolContext, ToolResult


@pytest.fixture(autouse=True)
def no_wake(monkeypatch):
    monkeypatch.setattr("loom.conversation.wake", lambda app, cid: None)


@pytest.fixture
def bus_events(app):
    seen: list[dict] = []
    app.bus.listen(seen.append)
    return seen


@pytest.fixture
def ctx(app):
    return ToolContext(
        app=app, conversation=app.conversation(app.desk_id()), call_id="call_1", event_id=11, request_text="do it"
    )


@pytest.fixture
def echo(app):
    async def handler(ctx, args):
        return ToolResult(content="echo")

    tool = Tool(
        name="echo",
        description="echo",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        handler=handler,
    )
    app.tools.register(tool, replace=True)
    permissions.set_policy(app, tool, "ask")
    return tool


def _call(name: str, arguments: dict, call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _notifications(app, conversation_id: str | None = None) -> list[dict]:
    """Every approval item, optionally for one conversation. Sorted by id for a stable
    read, never to assert creation order: `ulid()` is not monotonic within a millisecond
    (see `loom.ids`), so a test that wants a particular item names its conversation."""
    rows = [i for i in items.list_items(app, include_dismissed=True) if i["source"].startswith("approval:")]
    if conversation_id is not None:
        rows = [i for i in rows if i["conversation_id"] == conversation_id]
    return sorted(rows, key=lambda i: i["id"])


def _thread(app, title: str, goal: str) -> dict:
    cid = ulid()
    now = db.now_iso()
    app.db.execute(
        "INSERT INTO conversations(id, kind, parent_id, title, goal, pinned_notes, roots, depth, role, "
        "status, created_at, updated_at) VALUES (?, 'thread', ?, ?, ?, '[]', '[]', 1, 'worker', 'idle', ?, ?)",
        (cid, app.desk_id(), title, goal, now, now),
    )
    return app.conversation(cid)


# ---------------------------------------------------------------------------------
# one item per approval, on the interrupt path
# ---------------------------------------------------------------------------------
async def test_an_approval_posts_one_notification_item_naming_what_is_waiting(app, ctx, echo, bus_events):
    outcome = await dispatch.dispatch(app, ctx, _call("echo", {"text": "hi"}))
    assert isinstance(outcome, ApprovalNeeded)

    posted = _notifications(app)
    assert len(posted) == 1
    item = posted[0]
    assert item["kind"] == "notification"
    assert item["source"] == f"approval:{outcome.approval_id}"
    assert item["conversation_id"] == app.desk_id()
    assert "echo" in item["title"]  # the tool
    assert "Desk" in item["body"]  # the conversation, by title
    assert outcome.reason in item["body"]  # the reason
    assert item["action"] == {"kind": "open_conversation", "conversation_id": app.desk_id()}
    assert item["seen_at"] is None and item["dismissed_at"] is None

    notifications = [e for e in bus_events if e["type"] == "notification"]
    assert len(notifications) == 1
    assert notifications[0]["data"]["item_id"] == item["id"]
    assert notifications[0]["data"]["title"] == item["title"]


async def test_the_item_names_the_conversation_by_title_then_goal_then_id(app, echo):
    titled = _thread(app, "Port to raylib", "port the game")
    tctx = ToolContext(app=app, conversation=titled, call_id="c1", event_id=1, request_text="x")
    await dispatch.dispatch(app, tctx, _call("echo", {"text": "hi"}, call_id="c1"))
    assert "Port to raylib" in _notifications(app, titled["id"])[0]["body"]

    goal_only = _thread(app, "", "fix the build")
    gctx = ToolContext(app=app, conversation=goal_only, call_id="c2", event_id=2, request_text="x")
    await dispatch.dispatch(app, gctx, _call("echo", {"text": "hi"}, call_id="c2"))
    assert "fix the build" in _notifications(app, goal_only["id"])[0]["body"]

    bare = _thread(app, "", "")
    bctx = ToolContext(app=app, conversation=bare, call_id="c3", event_id=3, request_text="x")
    await dispatch.dispatch(app, bctx, _call("echo", {"text": "hi"}, call_id="c3"))
    assert bare["id"] in _notifications(app, bare["id"])[0]["body"]


async def test_two_approvals_give_two_items(app, ctx, echo):
    await dispatch.dispatch(app, ctx, _call("echo", {"text": "hi"}, call_id="call_1"))
    await dispatch.dispatch(app, ctx, _call("echo", {"text": "ho"}, call_id="call_2"))
    posted = _notifications(app)
    assert len(posted) == 2
    assert len({i["source"] for i in posted}) == 2


# ---------------------------------------------------------------------------------
# answering resolves it
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize("decision", ["allow", "allow_conversation", "allow_always", "deny"])
async def test_deciding_dismisses_the_item(app, ctx, echo, decision):
    outcome = await dispatch.dispatch(app, ctx, _call("echo", {"text": "hi"}))
    assert [i["id"] for i in items.unseen(app) if i["source"].startswith("approval:")]

    approvals.decide(app, outcome.approval_id, decision)

    item = _notifications(app)[0]
    assert item["dismissed_at"]
    # dismissed, so it is off the board entirely
    assert item["id"] not in [i["id"] for i in items.list_items(app)]


async def test_deciding_dismisses_only_its_own_item(app, ctx, echo):
    first = await dispatch.dispatch(app, ctx, _call("echo", {"text": "hi"}, call_id="call_1"))
    second = await dispatch.dispatch(app, ctx, _call("echo", {"text": "ho"}, call_id="call_2"))
    approvals.decide(app, first.approval_id, "allow")

    live = [i["source"] for i in items.list_items(app) if i["source"].startswith("approval:")]
    assert live == [f"approval:{second.approval_id}"]


async def test_the_internal_protected_path_approval_notifies_too(client, app, tmp_path):
    from loom.tools import fs_guard

    path = fs_guard.add_protected(app, str(tmp_path / "keys"))
    approval = (await client.delete("/api/protected-paths", params={"path": path})).json()["approval"]

    posted = _notifications(app)
    assert len(posted) == 1 and posted[0]["source"] == f"approval:{approval['id']}"
    assert approvals.INTERNAL_TOOL in posted[0]["title"]

    assert (await client.post(f"/api/approvals/{approval['id']}", json={"decision": "allow"})).status_code == 200
    assert _notifications(app)[0]["dismissed_at"]


# ---------------------------------------------------------------------------------
# by_source, the lookup the resolution uses
# ---------------------------------------------------------------------------------
def test_by_source_finds_the_rows_one_writer_posted(app):
    a = items.post(app, "notification", "one", source="approval:A")
    b = items.post(app, "notification", "two", source="approval:B")
    assert [r["id"] for r in items.by_source(app, "approval:A")] == [a["id"]]
    assert [r["id"] for r in items.by_source(app, "approval:B")] == [b["id"]]
    assert items.by_source(app, "approval:NOSUCH") == []

    items.dismiss(app, a["id"])
    assert [r["id"] for r in items.by_source(app, "approval:A")] == [a["id"]]  # dismissed rows included
    assert items.by_source(app, "approval:A")[0]["action"] == {}
