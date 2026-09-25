"""`knowledge_write(op="replace")` accepts `new` as the name for `content`
(tower-defense run audit §2 F9: the raylib thread sent `old` + `new` and lost a step).

The alias has to survive the dispatcher's own schema check, which is why `content` is no
longer in the tool's `required` list — the handler is the one place that knows the two
names mean the same field.
"""

from __future__ import annotations

from loom import conversation
from loom.tools import dispatch
from loom.tools.registry import ToolContext


def _ctx(app):
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1")


async def _call(app, args):
    tool = app.tools.get("knowledge_write")
    problems = dispatch.validate_args(tool, args)
    assert not problems, problems
    return await tool.handler(_ctx(app), args)


async def test_replace_accepts_new_as_content(app):
    app.vault.write("topics/raylib.md", "window: SetForegroundWindow works", op="create")
    result = await _call(app, {"path": "topics/raylib.md", "op": "replace",
                               "old": "SetForegroundWindow works", "new": "SetWindowPos works"})
    assert result.ok, result.content
    assert "SetWindowPos works" in app.vault.read("topics/raylib.md").body


async def test_content_still_wins_and_other_ops_still_require_it(app):
    app.vault.write("topics/raylib.md", "a", op="create")
    result = await _call(app, {"path": "topics/raylib.md", "op": "replace", "old": "a",
                               "content": "b", "new": "ignored"})
    assert result.ok and app.vault.read("topics/raylib.md").body.strip() == "b"
    missing = await _call(app, {"path": "topics/new-note.md", "op": "create"})
    assert not missing.ok and "content is required" in missing.content
    # `new` is a replace-only alias: create does not silently accept it
    still_missing = await _call(app, {"path": "topics/new-note.md", "op": "create", "new": "x"})
    assert not still_missing.ok and "content is required" in still_missing.content


async def test_replace_without_either_names_both_fields(app):
    app.vault.write("topics/raylib.md", "a", op="create")
    result = await _call(app, {"path": "topics/raylib.md", "op": "replace", "old": "a"})
    assert not result.ok and "`new` for op=replace" in result.content


def test_the_prompt_names_the_fields_beside_the_ops():
    from loom import prompts

    text = prompts.load("tools/knowledge_write")
    assert "`new` is accepted as the name for `content`" in text
    assert "another name for `content` under `op=replace`" in text
