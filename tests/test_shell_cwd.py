"""The shell's working directory (2026-10-12). The harness names `artifacts/<conversation>`
as the conversation's scratch folder, so a first call with that cwd must run even
though nothing had written there yet; and a cwd that does not exist fails in the
dispatcher's argument step — before the guard, the judge or an approval — so nobody is
asked to approve a call that cannot run."""

from __future__ import annotations

import shutil

from loom.artifacts import conversation_dir
from loom.models.fake import Scripted
from loom.models.types import ToolCall
from loom.tools import dispatch
from loom.tools.dispatch import ApprovalNeeded
from loom.tools.registry import ToolContext, ToolResult


def _thread_ctx(app) -> ToolContext:
    thread = {**app.conversation(app.desk_id()), "kind": "thread"}
    return ToolContext(app=app, conversation=thread, call_id="call_1", event_id=11, request_text="x")


def _approvals(app) -> int:
    return int(app.db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0])


async def test_the_first_call_in_the_scratch_folder_runs_before_it_exists(app, fake):
    ctx = _thread_ctx(app)
    scratch = conversation_dir(app, ctx.conversation_id)
    shutil.rmtree(scratch, ignore_errors=True)
    assert not scratch.exists()

    result = await dispatch.dispatch(app, ctx, ToolCall(id="c1", name="shell", arguments={"command": "echo hi", "cwd": str(scratch)}))
    assert isinstance(result, ToolResult) and result.ok, result.content
    assert "hi" in result.content
    assert scratch.is_dir()


async def test_a_missing_cwd_fails_before_anyone_is_asked(app, fake):
    fake.on("classify", lambda req: Scripted(content='{"decision":"ask","reason":"would ask"}'))
    ctx = _thread_ctx(app)
    for cwd in (str(app.home / "no-such-folder"), "C:/definitely/not/here/loom-test"):
        result = await dispatch.dispatch(app, ctx, ToolCall(id="c1", name="shell", arguments={"command": "mkdir build", "cwd": cwd}))
        assert not isinstance(result, ApprovalNeeded)
        assert not result.ok and "cwd does not exist" in result.content
    assert fake.calls == []  # the judge was never consulted
    assert _approvals(app) == 0  # and nobody was asked
    assert "policy" not in ctx.meta


async def test_an_existing_cwd_still_goes_through_the_policy(app, fake):
    fake.on("classify", lambda req: Scripted(content='{"decision":"ask","reason":"would ask"}'))
    ctx = _thread_ctx(app)
    outcome = await dispatch.dispatch(app, ctx, ToolCall(id="c1", name="shell", arguments={"command": "mkdir build", "cwd": str(app.home)}))
    assert isinstance(outcome, ApprovalNeeded)
    assert _approvals(app) == 1
