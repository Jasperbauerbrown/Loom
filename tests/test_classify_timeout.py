"""The permission judge is bounded and fails closed (decision 2026-09-21)."""

from __future__ import annotations

from loom.models.fake import Scripted
from loom.tools import classify
from loom.tools.registry import ToolContext


def _ctx(app):
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1", request_text="list files")


async def test_judge_request_carries_the_bound(app, fake):
    app.settings.set(app.db, "permissions.classifier_timeout_s", 30)
    fake.on("classify", lambda req: Scripted(content='{"decision":"allow","reason":"read"}'))
    decision, _ = await classify.judge(app, _ctx(app), app.tools.get("shell"), {"command": "ls"})
    assert decision == "allow"
    req = [c for c in fake.calls if c.purpose == "classify"][-1]
    assert req.params["timeout_s"] == 30 and req.params["stream_idle_s"] == 30


async def test_transient_failure_retries_once_then_asks(app, fake):
    calls = []

    def flaky(req):
        calls.append(req)
        return Scripted(outcome="transient", error="provider busy (HTTP 429)")

    fake.on("classify", flaky)
    decision, reason = await classify.judge(app, _ctx(app), app.tools.get("shell"), {"command": "ls"})
    assert decision == "ask" and "did not answer" in reason and "429" in reason
    assert len(calls) == 2


async def test_transient_then_ok_is_used(app, fake):
    answers = [Scripted(outcome="transient", error="timeout"), Scripted(content='{"decision":"ask","reason":"pushes"}')]
    fake.on("classify", lambda req: answers.pop(0))
    decision, reason = await classify.judge(app, _ctx(app), app.tools.get("shell"), {"command": "git push"})
    assert decision == "ask" and reason == "pushes"
