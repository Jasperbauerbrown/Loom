"""presets_list: the read-only preset enumerator (03 §9.2), and the
conversation_create prompt clause that points at it instead of the HTTP endpoint."""

from __future__ import annotations

import pytest

from loom import db, prompts
from loom.tools import permissions
from loom.tools.registry import ToolContext


@pytest.fixture
def ctx(app):
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="p1", event_id=1)


async def run(app, ctx, args=None):
    return await app.tools.get("presets_list").handler(ctx, args or {})


# ---------------------------------------------------------------------------------
# registration and permission posture (mirrors conversation_list / knowledge_search)
# ---------------------------------------------------------------------------------
def test_registered_read_only_allowed_everywhere(app):
    tool = app.tools.get("presets_list")
    assert tool is not None
    assert tool.read_only is True
    # desk, threads and subthreads — the same kinds conversation_list gets (the default).
    assert tool.kinds == ("desk", "thread")
    assert "preset" in tool.description.lower()
    mode, key = permissions.decide(app, tool, {})
    assert mode == "allow" and key == "presets_list"


# ---------------------------------------------------------------------------------
# the listing itself
# ---------------------------------------------------------------------------------
async def test_lists_enabled_presets_with_params_and_role_bindings(app, ctx):
    result = await run(app, ctx)
    assert result.ok
    body = result.content
    # conftest seeds the enabled presets 'fake' and 'fake-embed'.
    assert "fake | local-ollama | fake-model" in body
    assert "fake-embed" in body
    # The four params worth showing an agent, plus everything else the preset sets.
    assert "effective_ctx=32768" in body
    assert "max_tokens=1024" in body
    assert "temperature=0.2" in body
    assert result.meta["count"] == 2
    # Role bindings from model_roles.
    assert "role bindings:" in body
    assert "desk → fake" in body
    assert "embedding → fake-embed" in body
    assert "utility → (unset)" in body  # conftest leaves utility unbound


async def test_disabled_presets_are_excluded_and_marked_in_bindings(app, ctx):
    now = db.now_iso()
    with db.transaction(app.db):
        app.db.execute(
            "INSERT INTO presets(name, provider, model, params, enabled, created_at, updated_at) "
            "VALUES ('retired', 'local-ollama', 'old-model', '{}', 0, ?, ?)",
            (now, now),
        )
        app.db.execute("UPDATE model_roles SET preset='retired' WHERE role='utility'")
    result = await run(app, ctx)
    assert result.ok
    preset_section, bindings = result.content.split("role bindings:")
    assert "retired" not in preset_section  # disabled presets are excluded outright
    assert "utility → retired (not enabled)" in bindings  # but a dangling binding is marked
    assert "desk → fake" in bindings


# ---------------------------------------------------------------------------------
# prompts: conversation_create points at the tool, never at Loom's own API
# ---------------------------------------------------------------------------------
def test_conversation_create_prompt_points_at_the_tool_not_the_endpoint():
    text = prompts.load("tools/conversation_create")
    assert "run `presets_list` to list valid names and what each is for" in text
    assert "api/presets" not in text
    assert "http://" not in text


def test_presets_list_prompt_describes_the_tool():
    text = prompts.load("tools/presets_list")
    assert "enabled model presets" in text
    assert "role bindings" in text