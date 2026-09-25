"""Resuming a turn keeps its step count (TD audit F6).

`run_turn` re-enters at step 1 after an approval pause. Before this, the raylib thread
went 65 steps, paused on an `ask`, and came back with a fresh 100 ("I have limited
steps" then "I have plenty of steps"). The count now comes from the persisted
`tool_call` event's `meta["step"]`. A turn whose results were lost to a backend restart
is *not* a resume: it starts fresh, and the INTERRUPTED branch leaves the count at 0.
"""

from __future__ import annotations

from loom import conversation, db
from loom.conversation import events, runner
from loom.models.fake import Scripted
from loom.tools import approvals, permissions
from loom.tools.registry import Tool, ToolResult

BUDGET = 20


def _probe(app, name: str, mode: str) -> Tool:
    """A registered tool under an explicit policy. `ask` pauses the turn, `allow` runs."""

    async def handler(ctx, args):
        return ToolResult(content=f"{name} ran")

    tool = Tool(name=name, description=name, parameters={"type": "object", "properties": {}}, handler=handler)
    app.tools.register(tool, replace=True)
    permissions.set_policy(app, tool, mode)
    return tool


async def _run(app, cid):
    await runner.run_turn(app, cid)
    task = app.tasks.get(f"turn:{cid}")
    if task is not None and not task.done():
        await task


def _turn_calls(fake, cid):
    return [c for c in fake.calls if c.conversation_id == cid and c.purpose == "turn"]


def _remaining(req) -> int:
    """The thread budget block the request carried, as a number."""
    tails = [
        str(m.get("content", ""))
        for m in req.messages
        if m["role"] == "user" and "[steps remaining:" in str(m.get("content", ""))
    ]
    assert tails, "no [steps remaining] block in the request"
    return int(tails[-1].split("[steps remaining:")[1].split("]")[0])


def _thread(app, fake):
    app.settings.set(app.db, "conversation.step_budget_thread", BUDGET)
    fake.on("status", lambda req: Scripted(content='{"state":"working","goal":"g","done":"","doing":"d","next":"n","blockers":""}'))
    fake.default = lambda req: Scripted(content="ok")
    return conversation.create_thread(app, goal="build the thing", created_by="user")["id"]


# -- the approval resume ------------------------------------------------------------------

async def test_approval_resume_keeps_the_step_count(app, fake):
    cid = _thread(app, fake)
    _probe(app, "probe_free", "allow")
    _probe(app, "probe_ask", "ask")
    step = Scripted(content="working", tool_calls=[("probe_free", {})])
    fake.script(
        step,                                                                   # step 1
        step,                                                                   # step 2
        Scripted(content="now the risky one", tool_calls=[("probe_ask", {})]),  # step 3: pauses
        Scripted(content="done"),                                               # step 4, after the resume
    )
    await _run(app, cid)

    assert app.conversation(cid)["status"] == "waiting_approval"
    before = _turn_calls(fake, cid)
    assert len(before) == 3
    assert [_remaining(c) for c in before] == [BUDGET, BUDGET - 1, BUDGET - 2]
    tc = events.last(app, cid, 1, kinds=["tool_call"])[0]
    assert tc["meta"]["step"] == 3

    pending = approvals.pending(app, cid)
    assert len(pending) == 1
    approvals.decide(app, pending[0]["id"], "allow")
    await _run(app, cid)

    after = _turn_calls(fake, cid)
    assert len(after) == 4
    # the resumed turn continues the budget: 3 steps are spent, not 0
    assert _remaining(after[3]) == BUDGET - 3
    replay = [e["kind"] for e in events.list_events(app, cid, limit=100, kinds=list(events.REPLAY_KINDS))]
    assert replay == [
        "user", "tool_call", "tool_result", "tool_call", "tool_result", "tool_call",
        "tool_result", "assistant",
    ]


async def test_a_denied_resume_keeps_the_step_count_too(app, fake):
    """Deny is the other half of the same branch; the budget must not reset either."""
    cid = _thread(app, fake)
    _probe(app, "probe_ask", "ask")
    fake.script(
        Scripted(content="asking", tool_calls=[("probe_ask", {})]),
        Scripted(content="fine, stopping"),
    )
    await _run(app, cid)
    approvals.decide(app, approvals.pending(app, cid)[0]["id"], "deny")
    await _run(app, cid)
    calls = _turn_calls(fake, cid)
    assert len(calls) == 2
    assert _remaining(calls[1]) == BUDGET - 1


# -- the restart is not a resume ----------------------------------------------------------

async def test_interrupted_repair_starts_a_fresh_budget(app, fake):
    """A turn whose tool results were lost to a backend restart is not being resumed: the
    work it did is gone with the results, so the next turn gets the whole budget."""
    cid = _thread(app, fake)
    events.append(app, cid, "user", "build the thing")
    events.append(
        app, cid, "tool_call",
        db.dumps([{"id": "c1", "name": "fs_list", "arguments": {"path": str(app.vault_dir)}}]),
        {"text": "listing the vault", "step": 7},
    )
    events.update(app, cid, status="running")
    fake.script(Scripted(content="recovered"))
    await _run(app, cid)

    tr = events.list_events(app, cid, kinds=["tool_result"], limit=10)[0]
    assert runner.INTERRUPTED in tr["content"]
    calls = _turn_calls(fake, cid)
    assert _remaining(calls[0]) == BUDGET
