"""Connector-declared before/after-tool hooks (03 §9.3 step 4½, §10.2; audit #14).

A hook annotates a call or turns an allow into an ask; it can never deny, never change
what executes, and never break the call. Every connector here is written into an
isolated `LOOM_CONNECTORS_DIR`, so nothing in `connectors/` has to carry a hook.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from loom.models.types import ToolCall
from loom.tools import dispatch, hooks, permissions
from loom.tools.registry import Tool, ToolContext, ToolResult

from .test_connectors_loader import connectors_dir, write_connector  # noqa: F401  (fixture)


# ---------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------
@pytest.fixture
def ctx(app):
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="call_1", event_id=7, request_text="do the thing")


@pytest.fixture
def echo(app):
    """A registered, always-allowed tool."""

    async def handler(ctx, args):
        return ToolResult(content=f"echo:{args.get('text', '')}")

    tool = Tool(
        name="echo",
        description="echo",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        handler=handler,
        read_only=True,
    )
    app.tools.register(tool, replace=True)
    permissions.set_policy(app, tool, "allow")
    return tool


def marker(connector: str, note: str) -> str:
    return f"[{connector}] {note}"


HEADER = "from loom.connectors.base import Connector\n"


async def load(app, connectors_dir: Path, name: str, body: str) -> None:
    write_connector(connectors_dir, name, HEADER + body)
    app.connectors.discover()
    row = await app.connectors.apply(name)
    assert row["status"] == "loaded", row


# ---------------------------------------------------------------------------------
# before: ask, notes, and the reason that names the connector
# ---------------------------------------------------------------------------------
async def test_before_hook_turns_an_allow_into_an_ask(connectors_dir, app, ctx, echo):
    await load(
        app,
        connectors_dir,
        "asker",
        "async def before_tool(api, config, ctx_summary, tool_name, args):\n"
        "    return {'ask': True, 'reason': f'{tool_name} touches production'}\n"
        "CONNECTOR = Connector(name='asker', description='asks', before_tool=before_tool)\n",
    )
    verdict = await hooks.before(app, ctx, echo, {"text": "hi"})
    assert verdict.ask is True
    assert verdict.reason == "asker: echo touches production"
    assert verdict.notes == []


async def test_the_dispatcher_asks_when_a_hook_asks(connectors_dir, app, ctx, echo):
    await load(
        app,
        connectors_dir,
        "asker",
        "async def before_tool(api, config, ctx_summary, tool_name, args):\n"
        "    return {'ask': True, 'reason': 'ask me first'}\n"
        "CONNECTOR = Connector(name='asker', description='asks', before_tool=before_tool)\n",
    )
    out = await dispatch.dispatch(app, ctx, ToolCall(id="call_1", name="echo", arguments={"text": "hi"}))
    assert isinstance(out, dispatch.ApprovalNeeded)
    assert out.reason == "asker: ask me first"
    assert ctx.meta["decision"] == "ask"


async def test_a_hook_cannot_deny(connectors_dir, app, ctx, echo, caplog):
    await load(
        app,
        connectors_dir,
        "denier",
        "async def before_tool(api, config, ctx_summary, tool_name, args):\n"
        "    return {'decision': 'deny', 'reason': 'absolutely not'}\n"
        "CONNECTOR = Connector(name='denier', description='tries to deny', before_tool=before_tool)\n",
    )
    with caplog.at_level("WARNING"):
        verdict = await hooks.before(app, ctx, echo, {"text": "hi"})
    assert verdict.ask is True  # the strongest a hook may be: an ask, never a denial
    assert verdict.reason == "denier: absolutely not"
    assert "tried to deny" in caplog.text


async def test_hooks_see_a_summary_and_a_copy_of_the_args(connectors_dir, app, ctx, echo):
    await load(
        app,
        connectors_dir,
        "peeker",
        "SEEN = []\n"
        "async def before_tool(api, config, ctx_summary, tool_name, args):\n"
        "    SEEN.append((dict(ctx_summary), dict(args)))\n"
        "    args['text'] = 'mutated'\n"
        "    return None\n"
        "CONNECTOR = Connector(name='peeker', description='looks', before_tool=before_tool)\n",
    )
    ctx.meta.update({"policy": "classify", "decision": "allow", "reason": "read-only"})
    ctx.intent = "checking the echo"
    args = {"text": "hi", "token": "{{secret:API_KEY}}"}
    verdict = await hooks.before(app, ctx, echo, args)
    assert verdict.ask is False

    module = app.connectors.modules["peeker"]
    summary, seen_args = module.SEEN[0]
    assert summary["conversation_id"] == app.desk_id()
    assert summary["conversation_kind"] == "desk"
    assert summary["call_id"] == "call_1"
    assert summary["intent"] == "checking the echo"
    assert summary["request_text"] == "do the thing"
    assert (summary["policy"], summary["decision"], summary["reason"]) == ("classify", "allow", "read-only")
    assert seen_args["token"] == "{{secret:API_KEY}}"  # placeholders, never a real secret
    assert args["text"] == "hi"  # the hook mutated its own copy


# ---------------------------------------------------------------------------------
# notes: before → ctx.meta["hook_notes"] → the result content, marker-formatted
# ---------------------------------------------------------------------------------
async def test_notes_from_before_and_after_reach_the_result(connectors_dir, app, ctx, echo):
    await load(
        app,
        connectors_dir,
        "noter",
        "async def before_tool(api, config, ctx_summary, tool_name, args):\n"
        "    return {'notes': ['before note', '  ']}\n"
        "async def after_tool(api, config, ctx_summary, tool_name, args, result):\n"
        "    return {'notes': ['after note: ok=%s' % result['ok']]}\n"
        "CONNECTOR = Connector(name='noter', description='notes', before_tool=before_tool, after_tool=after_tool)\n",
    )
    verdict = await hooks.before(app, ctx, echo, {"text": "hi"})
    assert verdict.ask is False
    assert verdict.notes == [marker("noter", "before note")]

    out = await dispatch.dispatch(app, ctx, ToolCall(id="call_1", name="echo", arguments={"text": "hi"}))
    assert isinstance(out, ToolResult) and out.ok
    assert ctx.meta["hook_notes"] == [marker("noter", "before note")]
    assert out.content.splitlines() == ["echo:hi", marker("noter", "before note"), marker("noter", "after note: ok=True")]


async def test_before_notes_land_even_without_an_after_hook(connectors_dir, app, ctx, echo):
    await load(
        app,
        connectors_dir,
        "only_before",
        "def before_tool(api, config, ctx_summary, tool_name, args):\n"  # a sync hook is accepted
        "    return {'notes': ['said once']}\n"
        "CONNECTOR = Connector(name='only_before', description='notes', before_tool=before_tool)\n",
    )
    ctx.meta["hook_notes"] = (await hooks.before(app, ctx, echo, {"text": "hi"})).notes
    result = await hooks.after(app, ctx, echo, {"text": "hi"}, ToolResult(content="echo:hi"))
    assert result.content == "echo:hi\n" + marker("only_before", "said once")
    assert result.ok is True and result.meta == {}


async def test_after_never_swallows_the_result(connectors_dir, app, ctx, echo):
    await load(
        app,
        connectors_dir,
        "meddler",
        "async def after_tool(api, config, ctx_summary, tool_name, args, result):\n"
        "    result['ok'] = False\n"
        "    result['meta']['sneaky'] = True\n"
        "    return {'ok': False, 'content': 'replaced', 'meta': {'sneaky': True}}\n"
        "CONNECTOR = Connector(name='meddler', description='meddles', after_tool=after_tool)\n",
    )
    result = await hooks.after(app, ctx, echo, {"text": "hi"}, ToolResult(content="echo:hi", meta={"exit_code": 0}))
    assert result.content == "echo:hi"  # no notes, and nothing else changed
    assert result.ok is True
    assert result.meta == {"exit_code": 0}


# ---------------------------------------------------------------------------------
# failure classes: raising, hanging, the wrong type
# ---------------------------------------------------------------------------------
async def test_a_hook_that_raises_is_logged_and_ignored(connectors_dir, app, ctx, echo, caplog):
    await load(
        app,
        connectors_dir,
        "exploder",
        "async def before_tool(api, config, ctx_summary, tool_name, args):\n"
        "    raise RuntimeError('hook boom')\n"
        "async def after_tool(api, config, ctx_summary, tool_name, args, result):\n"
        "    raise RuntimeError('hook boom')\n"
        "CONNECTOR = Connector(name='exploder', description='explodes', before_tool=before_tool, after_tool=after_tool)\n",
    )
    with caplog.at_level("WARNING"):
        verdict = await hooks.before(app, ctx, echo, {"text": "hi"})
        out = await dispatch.dispatch(app, ctx, ToolCall(id="call_1", name="echo", arguments={"text": "hi"}))
    assert (verdict.ask, verdict.reason, verdict.notes) == (False, "", [])
    assert isinstance(out, ToolResult) and out.ok and out.content == "echo:hi"
    assert "hook boom" in caplog.text


async def test_a_hook_that_hangs_is_bounded(connectors_dir, app, ctx, echo, monkeypatch, caplog):
    await load(
        app,
        connectors_dir,
        "sleeper",
        "import asyncio\n"
        "async def before_tool(api, config, ctx_summary, tool_name, args):\n"
        "    await asyncio.sleep(60)\n"
        "    return {'ask': True, 'reason': 'too late'}\n"
        "CONNECTOR = Connector(name='sleeper', description='hangs', before_tool=before_tool)\n",
    )
    monkeypatch.setattr(hooks, "HOOK_TIMEOUT_S", 0.02)
    with caplog.at_level("WARNING"):
        verdict = await hooks.before(app, ctx, echo, {"text": "hi"})
    assert verdict.ask is False and verdict.notes == []
    assert "timed out" in caplog.text


async def test_a_hook_returning_the_wrong_type_is_ignored(connectors_dir, app, ctx, echo, caplog):
    await load(
        app,
        connectors_dir,
        "confused",
        "async def before_tool(api, config, ctx_summary, tool_name, args):\n"
        "    return 'ask please'\n"
        "async def after_tool(api, config, ctx_summary, tool_name, args, result):\n"
        "    return ['a note']\n"
        "CONNECTOR = Connector(name='confused', description='wrong types', before_tool=before_tool, after_tool=after_tool)\n",
    )
    with caplog.at_level("WARNING"):
        verdict = await hooks.before(app, ctx, echo, {"text": "hi"})
        result = await hooks.after(app, ctx, echo, {"text": "hi"}, ToolResult(content="echo:hi"))
    assert verdict.ask is False and verdict.notes == []
    assert result.content == "echo:hi"
    assert "not a dict" in caplog.text


async def test_no_connectors_is_the_empty_verdict(app, ctx, echo):
    verdict = await hooks.before(app, ctx, echo, {"text": "hi"})
    assert (verdict.ask, verdict.reason, verdict.notes) == (False, "", [])
    result = await hooks.after(app, ctx, echo, {"text": "hi"}, ToolResult(content="echo:hi"))
    assert result.content == "echo:hi"


async def test_a_partial_app_without_connectors_is_fine(app, ctx, echo, monkeypatch):
    """The CLI and some tests build an app with no loader at all."""
    monkeypatch.setattr(app, "connectors", None)
    verdict = await hooks.before(app, ctx, echo, {"text": "hi"})
    assert verdict.ask is False
    result = await hooks.after(app, ctx, echo, {"text": "hi"}, ToolResult(content="echo:hi"))
    assert result.content == "echo:hi"


# ---------------------------------------------------------------------------------
# several connectors, and the lifecycle
# ---------------------------------------------------------------------------------
async def test_two_connectors_are_both_consulted_in_a_stable_order(connectors_dir, app, ctx, echo):
    for name, note in (("zeta", "last"), ("alpha", "first")):
        await load(
            app,
            connectors_dir,
            name,
            f"async def before_tool(api, config, ctx_summary, tool_name, args):\n"
            f"    return {{'ask': True, 'reason': '{note}', 'notes': ['{note}']}}\n"
            f"CONNECTOR = Connector(name='{name}', description='both', before_tool=before_tool)\n",
        )
    verdict = await hooks.before(app, ctx, echo, {"text": "hi"})
    assert verdict.ask is True
    assert verdict.reason == "alpha: first; zeta: last"
    assert verdict.notes == [marker("alpha", "first"), marker("zeta", "last")]


async def test_disable_and_reload_take_the_hooks_with_them(connectors_dir, app, ctx, echo):
    await load(
        app,
        connectors_dir,
        "toggler",
        "async def after_tool(api, config, ctx_summary, tool_name, args, result):\n"
        "    return {'notes': ['still here']}\n"
        "CONNECTOR = Connector(name='toggler', description='toggles', after_tool=after_tool)\n",
    )
    assert [r.name for r in app.connectors.hooks()] == ["toggler"]

    await app.connectors.enable("toggler", False)
    assert app.connectors.hooks() == []
    result = await hooks.after(app, ctx, echo, {"text": "hi"}, ToolResult(content="echo:hi"))
    assert result.content == "echo:hi"

    await app.connectors.enable("toggler", True)
    assert [r.name for r in app.connectors.hooks()] == ["toggler"]
    await app.connectors.reload("toggler")
    assert [r.name for r in app.connectors.hooks()] == ["toggler"]
    result = await hooks.after(app, ctx, echo, {"text": "hi"}, ToolResult(content="echo:hi"))
    assert result.content == "echo:hi\n" + marker("toggler", "still here")


async def test_the_hooks_record_carries_the_api_and_the_validated_config(connectors_dir, app):
    from loom.connectors.base import SurfaceApi

    write_connector(
        connectors_dir,
        "configured",
        HEADER + "async def before_tool(api, config, ctx_summary, tool_name, args):\n"
        "    return None\n"
        "CONNECTOR = Connector(name='configured', description='configured',\n"
        "    config_schema={'type': 'object', 'properties': {'label': {'type': 'string', 'default': 'plain'}}},\n"
        "    before_tool=before_tool)\n",
    )
    app.connectors.discover()
    await app.connectors.apply("configured")
    record = app.connectors.hooks()[0]
    assert record.name == "configured"
    assert isinstance(record.api, SurfaceApi) and record.api.name == "configured"
    assert record.config == {"label": "plain"}  # defaults filled, as `apply` validated it
    assert record.after is None


async def test_facets_include_hooks(connectors_dir, app):
    await load(
        app,
        connectors_dir,
        "faceted",
        "async def after_tool(api, config, ctx_summary, tool_name, args, result):\n"
        "    return None\n"
        "CONNECTOR = Connector(name='faceted', description='hooks only', after_tool=after_tool)\n",
    )
    assert app.connectors.describe("faceted")["facets"] == ["hooks"]


# ---------------------------------------------------------------------------------
# the known shape: an approved call runs the after-hooks only (03 §9.3)
# ---------------------------------------------------------------------------------
async def test_an_approved_call_runs_only_the_after_hooks(connectors_dir, app, ctx, echo):
    """`before` cannot loop: the runner resumes an approved call through
    `dispatch.execute`, which is steps 5-7 and runs the after-hooks alone."""
    await load(
        app,
        connectors_dir,
        "counter",
        "BEFORE = []\n"
        "async def before_tool(api, config, ctx_summary, tool_name, args):\n"
        "    BEFORE.append(tool_name)\n"
        "    return {'ask': True, 'reason': 'check with me'}\n"
        "async def after_tool(api, config, ctx_summary, tool_name, args, result):\n"
        "    return {'notes': ['ran after approval']}\n"
        "CONNECTOR = Connector(name='counter', description='counts', before_tool=before_tool, after_tool=after_tool)\n",
    )
    out = await dispatch.dispatch(app, ctx, ToolCall(id="call_1", name="echo", arguments={"text": "hi"}))
    assert isinstance(out, dispatch.ApprovalNeeded)

    resumed = ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="call_1", event_id=7)
    result = await dispatch.execute(app, resumed, echo, {"text": "hi"})
    assert result.content == "echo:hi\n" + marker("counter", "ran after approval")
    assert app.connectors.modules["counter"].BEFORE == ["echo"]  # once, not twice


async def test_a_hook_does_not_hold_up_a_concurrent_turn(connectors_dir, app, ctx, echo, monkeypatch):
    """The bound is per call, so one slow connector costs one timeout, not the turn."""
    await load(
        app,
        connectors_dir,
        "sleeper",
        "import asyncio\n"
        "async def before_tool(api, config, ctx_summary, tool_name, args):\n"
        "    await asyncio.sleep(60)\n"
        "CONNECTOR = Connector(name='sleeper', description='hangs', before_tool=before_tool)\n",
    )
    monkeypatch.setattr(hooks, "HOOK_TIMEOUT_S", 0.02)
    verdicts = await asyncio.gather(*(hooks.before(app, ctx, echo, {"text": "hi"}) for _ in range(3)))
    assert all(v.ask is False for v in verdicts)
