"""«Allow this call for the conversation» (03 §9.3, TD audit F1).

A third approval answer between allow-once and `allow_always`: a `call_grants` row keyed
on (conversation, tool, args hash). It grants exactly one call shape in exactly one
conversation — nothing inherits, nothing static is widened, and the fs guard still gets
the first word.
"""

from __future__ import annotations

import pytest

from loom import db
from loom.ids import ulid
from loom.models.types import ToolCall
from loom.tools import approvals, dispatch, permissions
from loom.tools.dispatch import ApprovalNeeded
from loom.tools.registry import Tool, ToolContext, ToolResult


# ---------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def no_wake(monkeypatch):
    """No runner is listening in these tests; `decide` still wakes, so stub it out."""
    woken: list[str] = []
    monkeypatch.setattr("loom.conversation.wake", lambda app, cid: woken.append(cid))
    return woken


@pytest.fixture
def ctx(app):
    return ToolContext(
        app=app, conversation=app.conversation(app.desk_id()), call_id="call_1", event_id=11, request_text="do it"
    )


@pytest.fixture
def echo(app):
    """A registered tool whose policy the tests set; it records what it was given."""
    calls: list[dict] = []

    async def handler(ctx, args):
        calls.append(args)
        return ToolResult(content=f"echo:{args.get('text', '')}")

    tool = Tool(
        name="echo",
        description="echo",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        handler=handler,
    )
    app.tools.register(tool, replace=True)
    permissions.set_policy(app, tool, "ask")
    tool.seen = calls  # type: ignore[attr-defined]
    return tool


def _call(name: str, arguments: dict, call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _thread(app, title: str = "Port to raylib") -> dict:
    """A second conversation, written straight to the table: `create_thread` would post
    a goal and schedule a turn, and these tests only need another id."""
    cid = ulid()
    now = db.now_iso()
    app.db.execute(
        "INSERT INTO conversations(id, kind, parent_id, title, goal, pinned_notes, roots, depth, role, "
        "status, created_at, updated_at) VALUES (?, 'thread', ?, ?, 'port it', '[]', '[]', 1, 'worker', "
        "'idle', ?, ?)",
        (cid, app.desk_id(), title, now, now),
    )
    return app.conversation(cid)


# ---------------------------------------------------------------------------------
# args_hash
# ---------------------------------------------------------------------------------
def test_args_hash_is_canonical_and_argument_sensitive():
    assert permissions.args_hash({"a": 1, "b": 2}) == permissions.args_hash({"b": 2, "a": 1})
    assert permissions.args_hash(None) == permissions.args_hash({})
    assert permissions.args_hash({"text": "hi"}) != permissions.args_hash({"text": "ho"})
    assert len(permissions.args_hash({})) == 64


# ---------------------------------------------------------------------------------
# decide writes the row; the lookup answers for that call and no other
# ---------------------------------------------------------------------------------
async def test_allow_conversation_writes_the_grant_and_only_matches_that_call(app, ctx, echo):
    outcome = await dispatch.dispatch(app, ctx, _call("echo", {"text": "hi"}))
    assert isinstance(outcome, ApprovalNeeded)

    row = approvals.decide(app, outcome.approval_id, "allow_conversation")
    assert row["status"] == "allowed" and row["decision"] == "allow_conversation"

    stored = app.db.execute("SELECT * FROM call_grants").fetchall()
    assert len(stored) == 1
    assert stored[0]["conversation_id"] == app.desk_id()
    assert stored[0]["tool"] == "echo"
    assert stored[0]["args_hash"] == permissions.args_hash({"text": "hi"})
    assert stored[0]["approval_id"] == outcome.approval_id
    assert stored[0]["created_at"]

    reason = permissions.conversation_grant(app, app.desk_id(), "echo", {"text": "hi"})
    assert reason == f"allowed for this conversation (approval {outcome.approval_id})"
    assert permissions.conversation_grant(app, app.desk_id(), "echo", {"text": "ho"}) is None
    assert permissions.conversation_grant(app, app.desk_id(), "echo", {}) is None
    assert permissions.conversation_grant(app, app.desk_id(), "shell", {"text": "hi"}) is None


async def test_allow_conversation_wakes_the_conversation_like_allow(app, ctx, echo, no_wake):
    seen: list[dict] = []
    app.bus.listen(seen.append)
    outcome = await dispatch.dispatch(app, ctx, _call("echo", {"text": "hi"}))
    approvals.decide(app, outcome.approval_id, "allow_conversation")
    assert no_wake == [app.desk_id()]
    assert any(e["type"] == "approval.resolved" for e in seen)


async def test_granting_the_same_call_shape_twice_keeps_one_row(app, ctx, echo):
    """Two asks for the same call (a restart, a re-issued call) both answered this way:
    the second insert is a no-op, not a constraint error."""
    first = await dispatch.dispatch(app, ctx, _call("echo", {"text": "hi"}))
    approvals.decide(app, first.approval_id, "allow_conversation")
    app.db.execute("DELETE FROM call_grants")  # as if the grant had been cleared
    second = await dispatch.dispatch(app, ctx, _call("echo", {"text": "hi"}, call_id="call_2"))
    assert isinstance(second, ApprovalNeeded)
    approvals.decide(app, second.approval_id, "allow_conversation")

    app.db.execute(
        "INSERT OR IGNORE INTO call_grants(conversation_id, tool, args_hash, approval_id, created_at) "
        "VALUES (?, 'echo', ?, 'OTHER', ?)",
        (app.desk_id(), permissions.args_hash({"text": "hi"}), db.now_iso()),
    )
    rows = app.db.execute("SELECT * FROM call_grants").fetchall()
    assert len(rows) == 1 and rows[0]["approval_id"] == second.approval_id


# ---------------------------------------------------------------------------------
# the grant is per conversation: nothing inherits
# ---------------------------------------------------------------------------------
async def test_a_thread_grant_does_not_reach_the_desk_or_a_sibling(app, echo):
    thread = _thread(app)
    other = _thread(app, "Something else")
    tctx = ToolContext(app=app, conversation=thread, call_id="call_1", event_id=1, request_text="x")

    outcome = await dispatch.dispatch(app, tctx, _call("echo", {"text": "hi"}))
    approvals.decide(app, outcome.approval_id, "allow_conversation")

    assert permissions.conversation_grant(app, thread["id"], "echo", {"text": "hi"})
    assert permissions.conversation_grant(app, app.desk_id(), "echo", {"text": "hi"}) is None
    assert permissions.conversation_grant(app, other["id"], "echo", {"text": "hi"}) is None

    # and the desk still asks for the very same call
    dctx = ToolContext(
        app=app, conversation=app.conversation(app.desk_id()), call_id="call_2", event_id=2, request_text="x"
    )
    assert isinstance(await dispatch.dispatch(app, dctx, _call("echo", {"text": "hi"})), ApprovalNeeded)


async def test_a_desk_grant_does_not_reach_a_thread(app, ctx, echo):
    outcome = await dispatch.dispatch(app, ctx, _call("echo", {"text": "hi"}))
    approvals.decide(app, outcome.approval_id, "allow_conversation")

    thread = _thread(app)
    tctx = ToolContext(app=app, conversation=thread, call_id="call_2", event_id=2, request_text="x")
    assert isinstance(await dispatch.dispatch(app, tctx, _call("echo", {"text": "hi"})), ApprovalNeeded)


# ---------------------------------------------------------------------------------
# end to end through the dispatcher
# ---------------------------------------------------------------------------------
async def test_the_granted_call_runs_again_without_asking(app, ctx, echo):
    outcome = await dispatch.dispatch(app, ctx, _call("echo", {"text": "hi"}))
    assert isinstance(outcome, ApprovalNeeded) and echo.seen == []
    approvals.decide(app, outcome.approval_id, "allow_conversation")

    result = await dispatch.dispatch(app, ctx, _call("echo", {"text": "hi"}, call_id="call_2"))
    assert result.ok and result.content == "echo:hi"
    assert echo.seen == [{"text": "hi"}]
    assert ctx.meta["policy"] == "ask"
    assert ctx.meta["decision"] == "allow"
    assert ctx.meta["reason"] == f"allowed for this conversation (approval {outcome.approval_id})"

    # nothing static moved: the rule that asked still says ask, and no new rows appeared
    assert permissions.row_mode(app, "echo") == "ask"
    assert approvals.pending(app, app.desk_id()) == []

    # a different argument is a different call, so it asks again
    again = await dispatch.dispatch(app, ctx, _call("echo", {"text": "ho"}, call_id="call_3"))
    assert isinstance(again, ApprovalNeeded)


async def test_the_grant_does_not_widen_the_policy_or_the_roots(app, ctx, echo, tmp_path):
    before = dict(permissions.rows(app))
    roots_before = [r["path"] for r in app.db.execute("SELECT path FROM roots")]

    outcome = await dispatch.dispatch(app, ctx, _call("echo", {"text": "hi"}))
    approvals.decide(app, outcome.approval_id, "allow_conversation")

    assert permissions.rows(app) == before
    assert [r["path"] for r in app.db.execute("SELECT path FROM roots")] == roots_before


# ---------------------------------------------------------------------------------
# the guard keeps the first word
# ---------------------------------------------------------------------------------
async def test_a_protected_path_still_asks_after_a_grant(app, ctx):
    args = {"path": str(app.home / "secrets.json"), "content": "x"}
    first = await dispatch.dispatch(app, ctx, _call("fs_write", args))
    assert isinstance(first, ApprovalNeeded) and "protected path" in first.reason

    approvals.decide(app, first.approval_id, "allow_conversation")
    assert permissions.conversation_grant(app, app.desk_id(), "fs_write", args)

    # The fs guard runs at step 3, before the policy and before the grant is consulted.
    second = await dispatch.dispatch(app, ctx, _call("fs_write", args, call_id="call_2"))
    assert isinstance(second, ApprovalNeeded) and "protected path" in second.reason


# ---------------------------------------------------------------------------------
# fail closed
# ---------------------------------------------------------------------------------
async def test_the_decision_vocabulary(app, ctx, echo):
    assert approvals.DECISIONS == ("allow", "allow_conversation", "deny", "allow_always")
    outcome = await dispatch.dispatch(app, ctx, _call("echo", {"text": "hi"}))
    with pytest.raises(ValueError):
        approvals.decide(app, outcome.approval_id, "allow_for_this_conversation")
    with pytest.raises(ValueError):
        approvals.decide(app, outcome.approval_id, "")
    assert approvals.get(app, outcome.approval_id)["status"] == "pending"
    assert app.db.execute("SELECT COUNT(*) c FROM call_grants").fetchone()["c"] == 0


async def test_the_router_accepts_the_new_answer_and_lists_it_when_refusing(client, app):
    thread = _thread(client.loom)
    tctx = ToolContext(app=client.loom, conversation=thread, call_id="call_1", event_id=1, request_text="x")

    async def handler(ctx, args):
        return ToolResult("ran")

    tool = Tool(name="widget", description="d", parameters={"type": "object"}, handler=handler)
    client.loom.tools.register(tool, replace=True)
    permissions.set_policy(client.loom, tool, "ask")

    outcome = await dispatch.dispatch(client.loom, tctx, _call("widget", {"n": 1}))
    bad = await client.post(f"/api/approvals/{outcome.approval_id}", json={"decision": "sideways"})
    assert bad.status_code == 400 and "allow_conversation" in bad.json()["detail"]

    good = await client.post(f"/api/approvals/{outcome.approval_id}", json={"decision": "allow_conversation"})
    assert good.status_code == 200 and good.json()["decision"] == "allow_conversation"
    assert permissions.conversation_grant(client.loom, thread["id"], "widget", {"n": 1})
