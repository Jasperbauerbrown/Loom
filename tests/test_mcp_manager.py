"""`app.mcp` (03 §10.1): rows → live clients → registered tools, and the failure path.

Every server here is `tests/fake_mcp_server.py`, spawned for real.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from loom import items
from loom.connectors.mcp.manager import slug
from loom.tools.registry import ToolContext

SERVER = str(Path(__file__).parent / "fake_mcp_server.py")


async def make(app, name="fake", mode="normal", enabled=True, settle=True):
    """Create a row and settle the connect task `create` spawns for an enabled one, so
    the test sees the state the backend reaches on its own."""
    row = await app.mcp.create(name=name, transport="stdio", command=sys.executable, args=[SERVER, mode], enabled=enabled)
    task = app.tasks.get(f"mcp:{row['id']}")
    if task is not None and settle:
        await task
    return app.mcp.row(row["id"])


def ctx(app) -> ToolContext:
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1")


async def test_connect_registers_prefixed_tools_and_caches_them(app):
    row = await make(app)

    assert row["status"] == "connected"
    assert row["last_error"] is None
    assert [t["name"] for t in row["tools_cache"]] == ["fake_read", "fake_echo"]
    assert row["cached_at"]

    read = app.tools.get("mcp_fake_fake_read")
    echo = app.tools.get("mcp_fake_fake_echo")
    assert read is not None and echo is not None
    assert read.read_only is True and read.destructive is False
    assert echo.read_only is False and echo.destructive is True
    assert read.source == "mcp:fake"
    assert read.parameters["required"] == ["path"]

    # No policy rows: 03 §9.3 rule 3 decides (read-only → allow, else ask).
    assert app.db.execute("SELECT COUNT(*) c FROM tool_policies WHERE tool LIKE 'mcp_%'").fetchone()["c"] == 0

    result = await read.handler(ctx(app), {"path": "/x"})
    assert result.ok and result.content == "contents of /x"
    result = await echo.handler(ctx(app), {"fail": True})
    assert result.ok is False

    await app.mcp.disconnect(row["id"])


async def test_connect_writes_the_connector_note_with_a_tool_list(app):
    row = await make(app)
    note = app.vault.read("connectors/fake.md")
    assert note.frontmatter["kind"] == "mcp"
    assert "`mcp_fake_fake_read`" in note.body
    assert "<!-- tools:begin -->" in note.body
    await app.mcp.disconnect(row["id"])


async def test_failure_sets_error_posts_one_item_and_unregisters(app):
    row = await make(app, mode="exit")
    assert row["status"] == "error"
    assert "exited" in row["last_error"]
    assert "no credentials" in row["last_error"]
    assert app.tools.get("mcp_fake_fake_read") is None

    setup = [i for i in items.list_items(app) if i["kind"] == "setup"]
    assert len(setup) == 1
    assert setup[0]["title"] == "MCP server 'fake' failed"
    assert setup[0]["action"] == {"kind": "open_settings", "tab": "integrations"}

    # A retry is scheduled with backoff while the row stays enabled.
    task = app.tasks.get(f"mcp:retry:{row['id']}")
    assert task is not None and not task.done()
    task.cancel()

    # A second failure does not post a second item while the first is undismissed.
    app.shutting_down = True
    await app.mcp.connect(row["id"])
    assert len([i for i in items.list_items(app) if i["kind"] == "setup"]) == 1
    app.shutting_down = False


async def test_a_successful_reconnect_dismisses_the_failure_item(app):
    """The item this server posted on failure is answered once it connects (03 §11.3
    item lifecycle) — a retry that eventually works must not leave a stale complaint."""
    row = await make(app, mode="exit")
    setup = [i for i in items.list_items(app) if i["kind"] == "setup"]
    assert len(setup) == 1 and setup[0]["dismissed_at"] is None

    task = app.tasks.get(f"mcp:retry:{row['id']}")
    if task is not None:
        task.cancel()
    await app.mcp.update(row["id"], args=[SERVER, "normal"])
    await app.mcp.connect(row["id"])

    after = [i for i in items.list_items(app, include_dismissed=True) if i["kind"] == "setup"]
    assert len(after) == 1 and after[0]["dismissed_at"] is not None
    await app.mcp.disconnect(row["id"])


async def test_missing_secret_names_itself(app):
    row = await make(app, enabled=False)
    await app.mcp.update(row["id"], enabled=False, env={"TOKEN": "{{secret:NOPE}}"})
    app.shutting_down = True
    row = await app.mcp.connect(row["id"]) or app.mcp.row(row["id"])
    app.shutting_down = False
    assert row["status"] == "disconnected"  # disabled rows never connect

    await app.mcp.update(row["id"], enabled=True)
    row = app.mcp.row(row["id"])
    assert row["status"] == "error"
    assert "NOPE" in row["last_error"]
    task = app.tasks.get(f"mcp:retry:{row['id']}")
    if task is not None:
        task.cancel()


async def test_env_reaches_the_child_with_secrets_substituted(app):
    app.secrets.set("FAKE_TOKEN", "s3cret")
    row = await make(app, enabled=False)
    await app.mcp.update(row["id"], enabled=True, env={"TOKEN": "{{secret:FAKE_TOKEN}}"})
    transport = app.mcp.clients[row["id"]].transport
    assert transport.env == {"TOKEN": "s3cret"}
    await app.mcp.disconnect(row["id"])


async def test_tools_list_changed_triggers_a_refresh(app):
    row = await make(app)
    client = app.mcp.clients[row["id"]]
    app.tools.unregister("mcp_fake_fake_read")

    client._notification("notifications/tools/list_changed", {})
    await app.tasks[f"mcp:refresh:{row['id']}"]

    assert app.tools.get("mcp_fake_fake_read") is not None
    await app.mcp.disconnect(row["id"])


async def test_refresh_and_reload_keep_the_tools(app):
    row = await make(app)
    assert (await app.mcp.refresh(row["id"]))["status"] == "connected"
    assert (await app.mcp.reload(row["id"]))["status"] == "connected"
    assert app.tools.get("mcp_fake_fake_echo") is not None
    await app.mcp.disconnect(row["id"])


async def test_test_connection_reports_latency_and_tool_count(app):
    row = await make(app, enabled=False)
    out = await app.mcp.test(row["id"])
    assert out["ok"] is True and out["tools"] == 2 and out["error"] is None
    assert out["latency_ms"] >= 0
    assert row["id"] not in app.mcp.clients  # a throwaway client, not the live one

    bad = await make(app, name="broken", mode="exit", enabled=False)
    out = await app.mcp.test(bad["id"])
    assert out["ok"] is False and "exited" in out["error"]


async def test_delete_disconnects_and_unregisters(app):
    row = await make(app)
    assert await app.mcp.delete(row["id"]) is True
    assert app.mcp.row(row["id"]) is None
    assert app.tools.get("mcp_fake_fake_read") is None
    assert await app.mcp.delete(row["id"]) is False


async def test_rename_drops_the_old_tool_names(app):
    row = await make(app)
    await app.mcp.update(row["id"], name="Fake Two")
    assert app.tools.get("mcp_fake_fake_read") is None
    assert app.tools.get("mcp_fake_two_fake_read") is not None
    await app.mcp.disconnect(row["id"])


async def test_create_validates_shape_and_unique_name(app):
    with pytest.raises(ValueError):
        await app.mcp.create(name="x", transport="stdio", command="", enabled=False)
    with pytest.raises(ValueError):
        await app.mcp.create(name="x", transport="carrier-pigeon", command="c", enabled=False)
    await make(app, name="dupe", enabled=False)
    with pytest.raises(ValueError):
        await make(app, name="dupe", enabled=False)


async def test_start_never_blocks_on_a_slow_server(app):
    row = await make(app, mode="silent", settle=False)
    await asyncio.wait_for(app.mcp.start(), timeout=2)
    task = app.tasks.get(f"mcp:{row['id']}")
    assert task is not None
    task.cancel()
    await app.mcp.stop()


def test_slug():
    assert slug("Canvas LMS") == "canvas_lms"
    assert slug("weird!!name") == "weird_name"
    assert slug("") == "server"
