"""`ToolContext.intent`: what the turn said it was doing when it made the call (F1).

The classifier used to see only `request_text`, which for the desk is the last user
message. Seven identical `powershell -File <script>` calls were asked about because
"the 'Desk' goal does not clearly authorize" them - the desk has no goal, and "go ahead"
is not one. The runner now carries the step's own preamble, falling back to what the
same turn said earlier - never to an earlier turn (2026-09-25: a project-ideas reply from
22 hours before became the "stated intent" of `npm start`).
"""

from __future__ import annotations

from loom import conversation
from loom.conversation import events, runner
from loom.models.fake import Scripted
from loom.tools import approvals, permissions
from loom.tools.registry import Tool, ToolResult


def _probe(app, name: str, mode: str, seen: list[str]) -> Tool:
    """A tool that records the intent it was dispatched with."""

    async def handler(ctx, args):
        seen.append(ctx.intent)
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


async def test_intent_is_the_step_text_then_the_last_one(app, fake):
    desk = app.desk_id()
    seen: list[str] = []
    _probe(app, "probe", "allow", seen)
    fake.script(
        Scripted(content="Running the script I just wrote.", tool_calls=[("probe", {})]),
        Scripted(content="", tool_calls=[("probe", {})]),            # no preamble: the last one stands
        Scripted(content="Checking the output.", tool_calls=[("probe", {})]),
        Scripted(content="done"),
    )
    conversation.post(app, desk, "go ahead")
    await _run(app, desk)

    assert seen == [
        "Running the script I just wrote.",
        "Running the script I just wrote.",
        "Checking the output.",
    ]


async def test_a_turn_that_opens_with_a_bare_call_has_no_intent(app, fake):
    """A first step with no text has no intent of its own, and an earlier turn's reply is
    not one: the judge sees "(none stated)" rather than yesterday's conversation."""
    desk = app.desk_id()
    seen: list[str] = []
    _probe(app, "probe", "allow", seen)
    fake.script(Scripted(content="I will run the deploy script next."))
    conversation.post(app, desk, "hi")
    await _run(app, desk)

    fake.script(Scripted(content="", tool_calls=[("probe", {})]), Scripted(content="done"))
    conversation.post(app, desk, "go ahead")
    await _run(app, desk)

    assert seen == [""]


async def test_an_empty_transcript_gives_an_empty_intent(app, fake):
    desk = app.desk_id()
    seen: list[str] = []
    _probe(app, "probe", "allow", seen)
    fake.script(Scripted(content="", tool_calls=[("probe", {})]), Scripted(content="done"))
    conversation.post(app, desk, "go ahead")
    await _run(app, desk)

    assert seen == [""]


async def test_a_call_executed_after_an_approval_keeps_the_intent_it_was_made_with(app, fake):
    """The repair path builds its own ToolContext, so the intent comes from where it was
    persisted: the tool_call event's `meta["text"]`."""
    desk = app.desk_id()
    seen: list[str] = []
    _probe(app, "probe_ask", "ask", seen)
    _probe(app, "probe", "allow", seen)
    fake.script(
        Scripted(content="Installing the dependency now.", tool_calls=[("probe_ask", {})]),
        Scripted(content="", tool_calls=[("probe", {})]),  # after the resume, with no text of its own
        Scripted(content="done"),
    )
    conversation.post(app, desk, "go ahead")
    await _run(app, desk)
    assert app.conversation(desk)["status"] == "waiting_approval"
    assert seen == []
    tc = events.last(app, desk, 1, kinds=["tool_call"])[0]
    assert tc["meta"]["text"] == "Installing the dependency now."

    approvals.decide(app, approvals.pending(app, desk)[0]["id"], "allow")
    await _run(app, desk)

    # [0] the approved call, executed by _repair; [1] the next step, which has no text and
    # falls back to the resumed call's intent.
    assert seen == ["Installing the dependency now.", "Installing the dependency now."]


async def test_a_resumed_bare_call_takes_the_same_turns_earlier_text(app, fake):
    """After a restart the TurnState is empty; a resumed call with no preamble of its own
    takes the newest text its turn said before it."""
    desk = app.desk_id()
    seen: list[str] = []
    _probe(app, "probe_ask", "ask", seen)
    _probe(app, "probe", "allow", seen)
    fake.script(
        Scripted(content="Looking at the service first.", tool_calls=[("probe", {})]),
        Scripted(content="", tool_calls=[("probe_ask", {})]),
        Scripted(content="done"),
    )
    conversation.post(app, desk, "restart it")
    await _run(app, desk)
    assert app.conversation(desk)["status"] == "waiting_approval"

    approvals.decide(app, approvals.pending(app, desk)[0]["id"], "allow")
    await _run(app, desk)

    assert seen == ["Looking at the service first.", "Looking at the service first."]


async def test_a_resumed_bare_call_never_takes_an_earlier_turns_text(app, fake):
    desk = app.desk_id()
    seen: list[str] = []
    _probe(app, "probe_ask", "ask", seen)
    fake.script(Scripted(content="Here are some project ideas."))
    conversation.post(app, desk, "any ideas?")
    await _run(app, desk)

    fake.script(Scripted(content="", tool_calls=[("probe_ask", {})]), Scripted(content="done"))
    conversation.post(app, desk, "start the bot")
    await _run(app, desk)
    approvals.decide(app, approvals.pending(app, desk)[0]["id"], "allow")
    await _run(app, desk)

    assert seen == [""]


def _carried_probe(app, name: str, mode: str, seen: list[tuple[str, bool]]) -> None:
    """A tool that records the intent and whether it was carried from an earlier step."""

    async def handler(ctx, args):
        seen.append((ctx.intent, ctx.intent_carried))
        return ToolResult(content=f"{name} ran")

    tool = Tool(name=name, description=name, parameters={"type": "object", "properties": {}}, handler=handler)
    app.tools.register(tool, replace=True)
    permissions.set_policy(app, tool, mode)


async def test_a_carried_intent_is_labelled_as_such(app, fake):
    """"promoting now - this restarts me" from step 3 was read as the intent of five
    later read-only calls and made each look suspicious (2026-09-28): the fallback stays,
    but a call knows when its intent is an earlier step's text."""
    desk = app.desk_id()
    seen: list[tuple[str, bool]] = []
    _carried_probe(app, "cprobe", "allow", seen)
    fake.script(
        Scripted(content="Promoting now.", tool_calls=[("cprobe", {})]),
        Scripted(content="", tool_calls=[("cprobe", {})]),
        Scripted(content="Checking the log.", tool_calls=[("cprobe", {})]),
        Scripted(content="done"),
    )
    conversation.post(app, desk, "go ahead")
    await _run(app, desk)

    assert seen == [("Promoting now.", False), ("Promoting now.", True), ("Checking the log.", False)]


async def test_a_bare_first_step_carries_nothing(app, fake):
    desk = app.desk_id()
    seen: list[tuple[str, bool]] = []
    _carried_probe(app, "cprobe", "allow", seen)
    fake.script(Scripted(content="", tool_calls=[("cprobe", {})]), Scripted(content="done"))
    conversation.post(app, desk, "go ahead")
    await _run(app, desk)

    assert seen == [("", False)]


async def test_a_resumed_call_carries_only_when_it_had_no_text_of_its_own(app, fake):
    desk = app.desk_id()
    seen: list[tuple[str, bool]] = []
    _carried_probe(app, "cprobe_ask", "ask", seen)
    _carried_probe(app, "cprobe", "allow", seen)
    fake.script(
        Scripted(content="Installing it.", tool_calls=[("cprobe_ask", {})]),
        Scripted(content="", tool_calls=[("cprobe", {})]),
        Scripted(content="done"),
    )
    conversation.post(app, desk, "go ahead")
    await _run(app, desk)
    approvals.decide(app, approvals.pending(app, desk)[0]["id"], "allow")
    await _run(app, desk)

    # the approved call had its own preamble; the bare step after it carries that text
    assert seen == [("Installing it.", False), ("Installing it.", True)]


def _request_probe(app, seen: list[tuple[str, bool]]) -> None:
    async def handler(ctx, args):
        seen.append((ctx.request_text, ctx.request_from_user))
        return ToolResult(content="ok")

    tool = Tool(name="req_probe", description="req_probe", parameters={"type": "object", "properties": {}}, handler=handler)
    app.tools.register(tool, replace=True)
    permissions.set_policy(app, tool, "allow")


async def test_request_is_from_the_user_only_for_a_typed_desk_message(app, fake):
    """The classifier labels the request as the user's own words only when it is: the
    desk, and every claimed row came from the user (2026-09-25)."""
    desk = app.desk_id()
    seen: list[tuple[str, bool]] = []
    _request_probe(app, seen)

    fake.script(Scripted(content="", tool_calls=[("req_probe", {})]), Scripted(content="done"))
    conversation.post(app, desk, "please start the bot again")
    await _run(app, desk)

    fake.script(Scripted(content="", tool_calls=[("req_probe", {})]), Scripted(content="done"))
    conversation.post(app, desk, "start the bot", source="connector:discord")
    await _run(app, desk)

    thread = conversation.create_thread(app, goal="start the bot", parent_id=desk, created_by="desk")["id"]
    fake.script(Scripted(content="", tool_calls=[("req_probe", {})]), Scripted(content="done"))
    await _run(app, thread)

    assert seen[0] == ("please start the bot again", True)
    assert seen[1][1] is False
    assert seen[2][1] is False and "start the bot" in seen[2][0]


async def test_a_resumed_desk_call_keeps_the_users_request(app, fake):
    desk = app.desk_id()
    seen: list[tuple[str, bool]] = []
    _request_probe(app, seen)
    probe_seen: list[str] = []
    _probe(app, "probe_ask", "ask", probe_seen)
    fake.script(
        Scripted(content="Starting it.", tool_calls=[("probe_ask", {})]),
        Scripted(content="", tool_calls=[("req_probe", {})]),
        Scripted(content="done"),
    )
    conversation.post(app, desk, "please start the bot again")
    await _run(app, desk)
    approvals.decide(app, approvals.pending(app, desk)[0]["id"], "allow")
    await _run(app, desk)

    assert seen == [("please start the bot again", True)]


async def test_length_truncated_reply_runs_no_calls_and_turn_continues(app, fake):
    desk = app.desk_id()
    ran: list[str] = []
    _probe(app, "probe", "allow", ran)
    fake.script(
        Scripted(content="", tool_calls=[("probe", {"n": 1}), ("probe", {"n": 2})], finish_reason="length"),
        Scripted(content="recovered"),
    )
    conversation.post(app, desk, "go")
    await _run(app, desk)

    assert ran == []
    evs = events.list_events(app, desk, limit=1000)
    tc = [e for e in evs if e["kind"] == "tool_call"][0]
    assert tc["meta"]["finish_reason"] == "length"
    results = [e for e in evs if e["kind"] == "tool_result"]
    assert len(results) == 2 and all(e["meta"]["ok"] is False for e in results)
    assert "output-token limit" in results[0]["content"]
    assert [e for e in evs if e["kind"] == "assistant"][-1]["content"] == "recovered"


async def test_identical_calls_in_one_reply_run_once(app, fake):
    desk = app.desk_id()
    ran: list[str] = []
    _probe(app, "probe", "allow", ran)
    fake.script(
        Scripted(content="", tool_calls=[("probe", {"a": 1, "b": 2}), ("probe", {"b": 2, "a": 1}), ("probe", {"a": 2})]),
        Scripted(content="done"),
    )
    conversation.post(app, desk, "go")
    await _run(app, desk)

    assert len(ran) == 2
    results = [e for e in events.list_events(app, desk, limit=1000) if e["kind"] == "tool_result"]
    assert [e["meta"]["ok"] for e in results] == [True, False, True]
    assert "identical" in results[1]["content"]
