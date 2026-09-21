"""Vault folder deletion, "open in file manager", and clearing a conversation's context."""

from __future__ import annotations

import pytest

from loom.conversation import compaction, context, events
from loom.knowledge.vault import VaultError


def _make_notes(app):
    app.vault.write("devices/a.md", "Device A.", op="create", frontmatter={"title": "A", "aliases": ["device a"]})
    app.vault.write("devices/sub/b.md", "Device B.", op="create", frontmatter={"title": "B"})
    app.index.rebuild()


def test_delete_folder_removes_notes_in_one_commit(app):
    _make_notes(app)
    removed = app.vault.delete_folder("devices")
    assert removed == ["devices/a.md", "devices/sub/b.md"]
    assert not app.vault.exists("devices/a.md") and not (app.vault_dir / "devices").exists()
    assert app.db.execute("SELECT COUNT(*) FROM notes WHERE path LIKE 'devices/%'").fetchone()[0] == 0
    assert app.db.execute("SELECT COUNT(*) FROM aliases WHERE alias='device a'").fetchone()[0] == 0
    log = app.vault.git.log(n=3)
    assert any("delete folder devices/ (2 files)" in c["message"] for c in log)


def test_delete_folder_refuses_core_and_root(app):
    with pytest.raises(VaultError):
        app.vault.delete_folder("core")
    with pytest.raises(VaultError):
        app.vault.delete_folder("")
    with pytest.raises(VaultError):
        app.vault.delete_folder("nope")


async def test_delete_folder_endpoint(client):
    app = client.loom
    _make_notes(app)
    r = await client.delete("/api/vault/folder", params={"path": "devices/sub"})
    assert r.status_code == 200 and r.json()["deleted"] == ["devices/sub/b.md"]
    assert app.vault.exists("devices/a.md")
    r = await client.delete("/api/vault/folder", params={"path": "core"})
    assert r.status_code == 400


async def test_open_path_endpoint_is_bounded(client, monkeypatch):
    app = client.loom
    opened = []
    from loom.server.routers import system as system_router

    monkeypatch.setattr(system_router, "_open_in_file_manager", lambda target: opened.append(target))
    _make_notes(app)
    r = await client.post("/api/system/open", json={"path": "devices/a.md"})
    assert r.status_code == 200 and opened[-1].replace("\\", "/").endswith("/vault/devices")
    r = await client.post("/api/system/open", json={"path": "devices"})
    assert r.status_code == 200 and opened[-1].replace("\\", "/").endswith("/vault/devices")
    r = await client.post("/api/system/open", json={"path": "C:/Windows" if app.home.drive else "/etc"})
    assert r.status_code == 403
    r = await client.post("/api/system/open", json={"path": "devices/missing"})
    assert r.status_code == 404
    assert len(opened) == 2


def test_clear_cuts_replay_but_keeps_transcript(app):
    desk = app.desk_id()
    events.append(app, desk, "user", "old question about the ender 3")
    events.append(app, desk, "assistant", "old answer")
    before = context.assemble(app, app.conversation(desk), [], None)
    assert len(before.history) == 2
    row = compaction.clear(app, desk)
    assert row["summary"] == "" and row["summary_cutoff"] == 2
    after = context.assemble(app, app.conversation(desk), [], None)
    assert after.history == []
    kinds = [e["kind"] for e in events.list_events(app, desk)]
    assert kinds == ["user", "assistant", "compaction"]
    assert events.list_events(app, desk, kinds=["compaction"])[0]["meta"]["kind"] == "clear"
    # the old events are still there for episodic recall
    hits = app.index.transcript_search("ender")
    assert any(h["event_id"] == 1 for h in hits)


async def test_clear_endpoint_refuses_while_running(client):
    app = client.loom
    desk = app.desk_id()
    events.update(app, desk, status="running")
    r = await client.post(f"/api/conversations/{desk}/clear")
    assert r.status_code == 409
    events.update(app, desk, status="idle")
    r = await client.post(f"/api/conversations/{desk}/clear")
    assert r.status_code == 200 and r.json()["summary_cutoff"] == 0
