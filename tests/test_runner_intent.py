"""`ToolContext.intent`: what the turn said it was doing when it made the call (F1).

The classifier used to see only `request_text`, which for the desk is the last user
message. Seven identical `powershell -File <script>` calls were asked about because
"the 'Desk' goal does not clearly authorize" them - the desk has no goal, and "go ahead"
is not one. The runner now carries the step's own preamble, with a fallback chain so a
bare tool call is never intent-less.
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


async def test_a_turn_that_opens_with_a_bare_call_falls_back_to_the_transcript(app, fake):
    """A first step with no text has no intent of its own; the last thing the model said
    in this conversation (an assistant event, or an earlier step's preamble) is it."""
    desk = app.desk_id()
    seen: list[str] = []
    _probe(app, "probe", "allow", seen)
    fake.script(Scripted(content="I will run the deploy script next."))
    conversation.post(app, desk, "hi")
    await _run(app, desk)

    fake.script(Scripted(content="", tool_calls=[("probe", {})]), Scripted(content="done"))
    conversation.post(app, desk, "go ahead")
    await _run(app, desk)

    assert seen == ["I will run the deploy script next."]


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
    # falls back through an empty TurnState to the transcript.
    assert seen == ["Installing the dependency now.", "Installing the dependency now."]
