"""Native connector discovery, configuration and events (03 §10.2).

`connectors/example_echo` is the real reference package; the failure cases get their own
`LOOM_CONNECTORS_DIR` so a broken package never has to live in the repo.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from loom import items
from loom.connectors.base import ConnectorError, validate_config

NAME = "example_echo"


@pytest.fixture
def routed(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture `loom.triggers.router.route_connector_event`, whether or not the triggers
    package has landed yet (it is imported lazily, by name)."""
    calls: list[dict] = []
    try:
        from loom.triggers import router as module  # type: ignore[attr-defined]
    except ImportError:
        import loom.triggers as triggers

        module = types.ModuleType("loom.triggers.router")
        monkeypatch.setitem(sys.modules, "loom.triggers.router", module)
        monkeypatch.setattr(triggers, "router", module, raising=False)

    def fake(app, name, title, content, meta=None, conversation_id=None):
        calls.append({"name": name, "title": title, "content": content, "meta": meta, "conversation_id": conversation_id})

    monkeypatch.setattr(module, "route_connector_event", fake, raising=False)
    return calls


@pytest.fixture
def connectors_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated connectors folder. Requested *before* `app` so discovery sees it."""
    directory = tmp_path / "connectors"
    directory.mkdir()
    monkeypatch.setenv("LOOM_CONNECTORS_DIR", str(directory))
    return directory


def write_connector(directory: Path, name: str, body: str) -> None:
    package = directory / name
    package.mkdir()
    (package / "__init__.py").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------------
# the reference connector
# ---------------------------------------------------------------------------------
async def test_example_echo_is_discovered_and_enabled(app):
    row = app.connectors.describe(NAME)
    assert row is not None
    assert row["enabled"] is True
    assert row["available"] is True
    assert row["facets"] == ["tools", "events"]
    assert row["tools"] == [f"{NAME}_echo"]
    assert "emit_events" in row["config_schema"]["properties"]


async def test_apply_registers_the_prefixed_tool(app):
    await app.connectors.apply(NAME)
    assert app.connectors.row(NAME)["status"] == "loaded"
    tool = app.tools.get(f"{NAME}_echo")
    assert tool is not None
    assert tool.source == f"connector:{NAME}"
    assert tool.read_only is True
    result = await tool.handler(None, {"text": "hello"})
    assert result.content == "hello"


async def test_apply_writes_the_connector_note(app):
    await app.connectors.apply(NAME)
    note = app.vault.read(f"connectors/{NAME}.md")
    assert note.frontmatter["kind"] == "connector"
    assert f"`{NAME}_echo`" in note.body


async def test_disable_unregisters_the_tool(app):
    await app.connectors.apply(NAME)
    row = await app.connectors.enable(NAME, False)
    assert row["status"] == "disabled"
    assert app.tools.get(f"{NAME}_echo") is None
    row = await app.connectors.enable(NAME, True)
    assert row["status"] == "loaded"
    assert app.tools.get(f"{NAME}_echo") is not None


async def test_events_reach_the_router(app, routed):
    await app.connectors.set_config(NAME, {"emit_events": True, "interval_s": 0.01})
    for _ in range(200):
        if routed:
            break
        await asyncio.sleep(0.01)
    assert routed, "the event source produced nothing"
    first = routed[0]
    assert first["name"] == NAME
    assert first["title"].startswith(f"{NAME} tick")
    assert first["meta"]["count"] == 1
    assert first["conversation_id"] is None
    await app.connectors.enable(NAME, False)


async def test_events_go_to_the_configured_conversation(app, routed):
    await app.connectors.set_config(NAME, {"emit_events": True, "interval_s": 0.01, "conversation_id": app.desk_id()})
    for _ in range(200):
        if routed:
            break
        await asyncio.sleep(0.01)
    assert routed and routed[0]["conversation_id"] == app.desk_id()
    await app.connectors.enable(NAME, False)


async def test_reload_reimports_and_keeps_working(app):
    await app.connectors.apply(NAME)
    row = await app.connectors.reload(NAME)
    assert row["status"] == "loaded"
    assert app.tools.get(f"{NAME}_echo") is not None


async def test_set_config_rejects_bad_values(app):
    with pytest.raises(ConnectorError):
        await app.connectors.set_config(NAME, {"emit_events": "yes"})
    with pytest.raises(ConnectorError):
        await app.connectors.set_config(NAME, {"nonsense": 1})
    assert app.connectors.row(NAME)["config"] == {}


# ---------------------------------------------------------------------------------
# failure classes
# ---------------------------------------------------------------------------------
async def test_import_error_is_recorded_not_fatal(connectors_dir, app):
    write_connector(connectors_dir, "broken", "raise RuntimeError('boom')\n")
    write_connector(connectors_dir, "not_a_connector", "CONNECTOR = 42\n")
    app.connectors.discover()

    broken = app.connectors.describe("broken")
    assert broken["status"] == "error"
    assert "boom" in broken["last_error"]
    assert broken["available"] is False
    assert app.connectors.describe("not_a_connector")["status"] == "error"

    setup = sorted(i["title"] for i in items.list_items(app) if i["kind"] == "setup")
    assert setup == ["Connector 'broken' failed to load", "Connector 'not_a_connector' failed to load"]

    await app.connectors.start()  # a broken package does not stop the others
    assert app.connectors.describe("broken")["status"] == "error"


async def test_required_config_stops_at_needs_config(connectors_dir, app):
    write_connector(
        connectors_dir,
        "needy",
        "from loom.connectors.base import Connector\n"
        "CONNECTOR = Connector(name='needy', description='needs a token',\n"
        "    config_schema={'type': 'object', 'properties': {'token': {'type': 'string'}}, 'required': ['token']})\n",
    )
    app.connectors.discover()
    row = await app.connectors.apply("needy")
    assert row["status"] == "needs_config"
    assert row["missing_config"] == ["token"]
    assert [i["title"] for i in items.list_items(app) if i["kind"] == "setup"] == ["Connector 'needy' needs configuration"]

    row = await app.connectors.set_config("needy", {"token": "abc"})
    assert row["status"] == "loaded"


async def test_event_source_failure_marks_error_and_retries(connectors_dir, app, routed, monkeypatch):
    write_connector(
        connectors_dir,
        "flaky",
        "from loom.connectors.base import Connector, Event\n"
        "class Source:\n"
        "    async def events(self, config):\n"
        "        raise RuntimeError('source died')\n"
        "        yield Event(kind='x', title='x', content='x')\n"
        "CONNECTOR = Connector(name='flaky', description='fails', events=Source())\n",
    )
    monkeypatch.setattr("loom.connectors.loader.BACKOFF_START", 0.01)
    app.connectors.discover()
    await app.connectors.apply("flaky")
    for _ in range(200):
        if app.connectors.row("flaky")["status"] == "error":
            break
        await asyncio.sleep(0.01)
    row = app.connectors.row("flaky")
    assert row["status"] == "error"
    assert "source died" in row["last_error"]
    assert [i["title"] for i in items.list_items(app) if i["kind"] == "setup"] == ["Connector 'flaky' stopped"]
    await app.connectors.enable("flaky", False)


async def test_unknown_connector_is_a_key_error(app):
    with pytest.raises(KeyError):
        await app.connectors.enable("nope", True)
    assert app.connectors.describe("nope") is None


# ---------------------------------------------------------------------------------
# config validation
# ---------------------------------------------------------------------------------
def test_validate_config_defaults_types_and_enums():
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["a", "b"], "default": "a"},
            "count": {"type": "integer"},
            "on": {"type": "boolean"},
        },
        "required": ["count"],
    }
    assert validate_config(schema, {"count": 2}) == {"count": 2, "mode": "a"}
    with pytest.raises(ConnectorError):
        validate_config(schema, {})
    with pytest.raises(ConnectorError):
        validate_config(schema, {"count": "two"})
    with pytest.raises(ConnectorError):
        validate_config(schema, {"count": 1, "mode": "z"})
    with pytest.raises(ConnectorError):
        validate_config(schema, {"count": 1, "on": "true"})
    assert validate_config(schema, {"count": 1, "conversation_id": "x"})["conversation_id"] == "x"


# ---------------------------------------------------------------------------------
# the real router (no monkeypatch): the wiring of 03 §10.2 → §11.2
# ---------------------------------------------------------------------------------
async def test_event_reaches_the_real_trigger_router(app, monkeypatch):
    """One event, end to end: inbox row + now-board item on the desk."""
    from loom import conversation

    monkeypatch.setattr(conversation, "wake", lambda *a, **k: None)  # no turn in a loader test
    await app.connectors.set_config(NAME, {"emit_events": True, "interval_s": 0.01})
    desk = app.desk_id()
    for _ in range(200):
        rows = app.db.execute("SELECT * FROM inbox WHERE source=?", (f"connector:{NAME}",)).fetchall()
        if rows:
            break
        await asyncio.sleep(0.01)
    await app.connectors.enable(NAME, False)
    assert rows and rows[0]["conversation_id"] == desk
    assert [i["title"] for i in items.list_items(app) if i["source"] == f"connector:{NAME}"]
