"""Step 6's timeout: whose number wins, and whose deadline fires first (03 §9.3).

From the tower-defense run audit (F5): a shell call asked for 180 s, was cancelled by
the dispatcher at the tool default of 120 s, and its output was thrown away. So the
call's own `timeout_s` is honoured up to `conversation.tool_timeout_max_s`, and the
dispatcher's `wait_for` sits a grace margin *above* the effective timeout — the tool's
own deadline fires first and returns what it has.
"""

from __future__ import annotations

import asyncio

import pytest

from loom.tools import dispatch
from loom.tools.dispatch import DEFAULT_TIMEOUT_S
from loom.tools.native import shell as shell_tool
from loom.tools.registry import Tool, ToolContext, ToolResult


@pytest.fixture
def ctx(app):
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1", event_id=1)


def _shell(app):
    return app.tools.get("shell")


def _tool(name: str, **kwargs) -> Tool:
    params = kwargs.pop("parameters", {"type": "object"})
    return Tool(name=name, description="d", parameters=params, handler=kwargs.pop("handler", None), **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------
# timeout_for: the decision table
# ---------------------------------------------------------------------------------
def test_a_call_without_its_own_timeout_uses_the_per_kind_setting(app):
    assert dispatch.timeout_for(app, _shell(app)) == 120
    assert dispatch.timeout_for(app, _shell(app), {}) == 120
    assert dispatch.timeout_for(app, app.tools.get("http"), {}) == 60


def test_the_calls_own_timeout_is_honoured(app):
    assert dispatch.timeout_for(app, _shell(app), {"timeout_s": 180}) == 180
    assert dispatch.timeout_for(app, app.tools.get("http"), {"timeout_s": 45}) == 45


def test_the_calls_own_timeout_is_clamped_by_the_ceiling(app):
    assert dispatch.timeout_for(app, _shell(app), {"timeout_s": 99999}) == 900
    assert dispatch.timeout_for(app, app.tools.get("http"), {"timeout_s": 99999}) == 900


def test_the_ceiling_is_a_setting(app):
    app.settings.set(app.db, "conversation.tool_timeout_max_s", 300)
    assert dispatch.timeout_for(app, _shell(app), {"timeout_s": 99999}) == 300
    assert dispatch.timeout_for(app, _shell(app), {"timeout_s": 180}) == 180


@pytest.mark.parametrize("value", ["soon", "180s", 0, -5, None, True, False, [], {}])
def test_an_unusable_timeout_falls_back_to_the_default(app, value):
    """Junk is an input, not an error: the call still runs, at the configured default."""
    assert dispatch.timeout_for(app, _shell(app), {"timeout_s": value}) == 120


def test_the_other_kinds_are_unchanged(app):
    assert dispatch.timeout_for(app, _tool("own", timeout_s=7), {}) == 7
    assert dispatch.timeout_for(app, _tool("mcp_x_y", source="mcp:x"), {}) == 300
    assert dispatch.timeout_for(app, _tool("plain"), {}) == DEFAULT_TIMEOUT_S


def test_a_declared_timeout_argument_beats_the_tools_own(app):
    """Ordering: `tool.timeout_s` used to be checked first, which is how a call's own
    number could never win. The rule keys on the declared argument, not on the name."""
    declares = _tool(
        "declares",
        timeout_s=7,
        parameters={"type": "object", "properties": {"timeout_s": {"type": "number"}}},
    )
    assert dispatch.timeout_for(app, declares, {"timeout_s": 30}) == 30
    assert dispatch.timeout_for(app, declares, {}) == 7
    assert dispatch.timeout_for(app, _tool("own", timeout_s=7), {"timeout_s": 30}) == 7


# ---------------------------------------------------------------------------------
# the handler and the dispatcher must agree
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "args",
    [{}, {"timeout_s": 180}, {"timeout_s": 2.5}, {"timeout_s": 99999}, {"timeout_s": 0}, {"timeout_s": -5}, {"timeout_s": "soon"}],
)
def test_the_shell_handler_computes_the_same_timeout_as_the_dispatcher(app, args):
    """The duplication is two local lines in two files; this is what stops it drifting."""
    assert shell_tool.effective_timeout(app, args) == dispatch.timeout_for(app, _shell(app), args)


def test_they_still_agree_when_the_ceiling_moves(app):
    app.settings.set(app.db, "conversation.tool_timeout_max_s", 240)
    app.settings.set(app.db, "conversation.tool_timeout_shell_s", 30)
    for args in ({}, {"timeout_s": 600}, {"timeout_s": 15}):
        assert shell_tool.effective_timeout(app, args) == dispatch.timeout_for(app, _shell(app), args)
    assert shell_tool.effective_timeout(app, {"timeout_s": 600}) == 240
    assert shell_tool.effective_timeout(app, {}) == 30


# ---------------------------------------------------------------------------------
# the grace margin
# ---------------------------------------------------------------------------------
async def test_a_handler_that_bounds_itself_answers_past_its_own_deadline(app, ctx):
    """The point of the margin: shell kills its tree at the effective timeout and then
    returns what the command said. That answer must not be cancelled on its way out."""
    async def handler(ctx_, args):
        await asyncio.sleep(0.35)
        return ToolResult(content="what it said before the kill", ok=False, meta={"timeout": True})

    result = await dispatch.execute(app, ctx, _tool("bounded", timeout_s=0.2, handler=handler), {})
    assert result.content == "what it said before the kill"
    assert result.meta["timeout"] is True


async def test_a_handler_that_ignores_its_deadline_is_still_cancelled(app, ctx):
    async def handler(ctx_, args):
        await asyncio.sleep(30)
        return ToolResult("never")

    result = await dispatch.execute(app, ctx, _tool("hangs", timeout_s=0.05, handler=handler), {})
    assert not result.ok and result.meta["timeout"] is True
    assert "timed out after 0.05s" in result.content  # the message names the effective timeout
