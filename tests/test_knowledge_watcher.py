"""The watcher: external edits reach the index, new procedures reach the now board."""

from __future__ import annotations

import asyncio


def test_scan_once_indexes_an_external_edit_and_announces_it(app):
    app.vault.write("devices/ender-3.md", "A printer.", op="create")
    app.index.rebuild()
    seen: list[dict] = []
    app.bus.listen(lambda e: seen.append(e) if e["type"] == "knowledge.changed" else None)

    # Obsidian writes the file behind our back.
    target = app.vault.root / "devices" / "ender-3.md"
    target.write_text("---\ntitle: Ender 3\naliases: [big printer]\n---\nA printer with a glass bed.\n", encoding="utf-8")

    changed = app.watcher.scan_once()
    assert changed == ["devices/ender-3.md"]
    assert seen and seen[-1]["data"]["paths"] == ["devices/ender-3.md"]
    hits = app.index.search_fts("glass bed")
    assert [h["path"] for h in hits] == ["devices/ender-3.md"]
    assert app.index.alias_scan("the big printer is on") == ["devices/ender-3.md"]

    assert app.watcher.scan_once() == []  # nothing changed the second time


def test_scan_once_notices_a_new_and_a_removed_note(app):
    app.index.rebuild()
    (app.vault.root / "topics" / "new.md").write_text("fresh\n", encoding="utf-8")
    assert "topics/new.md" in app.watcher.scan_once()
    assert app.db.execute("SELECT 1 FROM notes WHERE path='topics/new.md'").fetchone()

    (app.vault.root / "topics" / "new.md").unlink()
    assert app.watcher.scan_once() == ["topics/new.md"]
    assert app.db.execute("SELECT 1 FROM notes WHERE path='topics/new.md'").fetchone() is None


def test_new_unapproved_procedure_posts_one_setup_item(app):
    app.index.rebuild()
    folder = app.vault.root / "procedures" / "third-party"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: third-party\ndescription: installed by hand\n---\nsteps\n", encoding="utf-8"
    )

    app.watcher.scan_once()
    rows = [r for r in app.db.execute("SELECT * FROM items WHERE kind='setup'")]
    assert len(rows) == 1
    assert rows[0]["title"] == "New procedure 'third-party' needs approval"
    from loom import db

    assert db.loads(rows[0]["action"])["kind"] == "approve_procedure"

    # Editing it again does not post a second item.
    (folder / "SKILL.md").write_text(
        "---\nname: third-party\ndescription: installed by hand\n---\nmore steps\n", encoding="utf-8"
    )
    app.watcher.scan_once()
    app.watcher._announced.clear()  # as if the backend had restarted
    app.watcher.scan_once()
    assert len([r for r in app.db.execute("SELECT 1 FROM items WHERE kind='setup'")]) == 1


def test_approving_a_procedure_dismisses_its_setup_item(app):
    """Approval is the completion signal for the "needs approval" item (03 §11.3 item
    lifecycle): once the person approves it, its row leaves the now board."""
    app.index.rebuild()
    folder = app.vault.root / "procedures" / "third-party"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: third-party\ndescription: installed by hand\n---\nsteps\n", encoding="utf-8"
    )
    app.watcher.scan_once()
    row = app.db.execute("SELECT id, dismissed_at FROM items WHERE kind='setup'").fetchone()
    assert row is not None and row["dismissed_at"] is None

    app.vault.approve_procedure("third-party")

    after = app.db.execute("SELECT dismissed_at FROM items WHERE id=?", (row["id"],)).fetchone()
    assert after["dismissed_at"] is not None


def test_an_approved_procedure_posts_nothing(app):
    app.vault.write(
        "procedures/mine/SKILL.md",
        "steps",
        op="create",
        frontmatter={"name": "mine", "description": "written by loom"},
    )
    app.index.rebuild()
    app.watcher.scan_once()
    assert [r for r in app.db.execute("SELECT 1 FROM items WHERE kind='setup'")] == []


async def test_start_and_stop_run_the_poll_loop(app):
    app.settings.set(app.db, "knowledge.vault_poll_seconds", 0.5)
    app.index.rebuild()
    app.watcher.start()
    assert "vault-watcher" in app.tasks
    app.watcher.stop()
    await asyncio.sleep(0)
    assert app.watcher.running is False


async def test_startup_rebuilds_a_stale_index_and_starts_the_watcher(app):
    from loom import knowledge

    app.vault.write("topics/x.md", "body", op="create")
    app.db.execute("DELETE FROM notes")
    assert app.index.stale() is True

    await knowledge.start(app)
    try:
        assert app.index.stale() is False
        assert app.watcher.running is True
    finally:
        await knowledge.stop(app)
