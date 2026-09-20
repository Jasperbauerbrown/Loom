"""Contract smoke tests for the skeleton: migrator, settings, bootstrap, fake client."""

from loom import db
from loom.config import FIELD_MAP, Settings
from loom.models.fake import Scripted
from loom.models.types import ChatRequest


def test_migrate_is_idempotent(app):
    assert db.current_version(app.db) >= 1
    assert db.migrate(app.db) == []


def test_bootstrap_defaults(app):
    assert app.desk_id()
    modes = {r["tool"]: r["mode"] for r in app.db.execute("SELECT tool, mode FROM tool_policies")}
    assert modes["shell"] == "classify" and modes["fs_read"] == "allow" and modes["secret_set"] == "ask"
    assert app.db.execute("SELECT COUNT(*) FROM protected_paths").fetchone()[0] >= 6
    assert app.vault_dir.exists()
    db.migrate(app.db)


def test_settings_roundtrip(app):
    assert app.settings.get("conversation.step_budget_desk") == 25
    app.settings.set(app.db, "conversation.step_budget_desk", "30")
    assert Settings.load(app.db).get("conversation.step_budget_desk") == 30
    schema = Settings.schema()
    assert any(f["key"] == "general.timezone" for f in schema)
    assert all(f["key"] in FIELD_MAP for f in schema)


def test_settings_validation(app):
    import pytest

    with pytest.raises(ValueError):
        app.settings.set(app.db, "conversation.compact_threshold", 5)
    with pytest.raises(KeyError):
        app.settings.set(app.db, "nope.nope", 1)


async def test_fake_client_streams_and_records(fake):
    fake.script(Scripted(content="hello world", reasoning="think", tool_calls=[("shell", {"command": "ls"})]))
    req = ChatRequest(messages=[{"role": "user", "content": "hi"}], purpose="turn")
    result = await fake.chat(req)
    assert result.ok and result.content == "hello world" and result.reasoning == "think"
    assert result.tool_calls[0].name == "shell" and result.tool_calls[0].arguments == {"command": "ls"}
    assert fake.calls[0] is req
    vec = (await fake.embed(["a b c"]))[0]
    assert abs(sum(v * v for v in vec) - 1.0) < 1e-6
