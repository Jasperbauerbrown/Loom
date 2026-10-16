"""The index: rebuild, incremental update, links, FTS, embeddings, core blocks."""

from __future__ import annotations

import asyncio
from array import array

import pytest

from loom.knowledge.index import cosine, fts_query


def small_vault(app) -> None:
    app.vault.write(
        "devices/ender-3.md",
        "A 3D printer in the workshop. Heating uses [[pla-print]] and [[missing-note]].",
        op="create",
        frontmatter={"aliases": ["ender 3", "the printer"]},
    )
    app.vault.write(
        "procedures/pla-print/SKILL.md",
        "Heat the bed to 60 and the nozzle to 200.",
        op="create",
        frontmatter={"name": "pla-print", "description": "Print PLA on a printer"},
    )
    app.vault.write(
        "projects/music-bot.md",
        "A discord bot that plays music. Runs on the [[ender-3]] host.",
        op="create",
        frontmatter={"aliases": ["music bot"]},
    )


def test_rebuild_counts_aliases_links_and_core_order(app):
    small_vault(app)
    result = app.index.rebuild()
    assert result["notes"] == len(app.vault.list())
    assert result["invalid"] == 0
    assert result["ms"] >= 0

    aliases = {r["alias"] for r in app.db.execute("SELECT alias FROM aliases WHERE path='devices/ender-3.md'")}
    assert {"ender 3", "the printer", "ender-3"} <= aliases

    resolved = {(r["src"], r["dst"]) for r in app.db.execute("SELECT src, dst FROM links WHERE resolved=1")}
    assert ("devices/ender-3.md", "procedures/pla-print/SKILL.md") in resolved
    assert ("projects/music-bot.md", "devices/ender-3.md") in resolved
    assert app.index.dangling() == [{"target": "missing-note", "sources": ["devices/ender-3.md"]}]

    core = [r["path"] for r in app.db.execute("SELECT path FROM notes WHERE core_order IS NOT NULL ORDER BY core_order")]
    assert core == ["core/about-me.md", "core/conventions.md", "core/preferences.md"]


def test_rebuild_records_invalid_notes_without_blocking(app):
    (app.vault.root / "topics" / "bad.md").write_text("---\nx: [1\n---\nbroken\n", encoding="utf-8")
    app.vault.write("topics/good.md", "fine", op="create")
    result = app.index.rebuild()
    assert result["invalid"] == 1
    row = app.db.execute("SELECT type FROM notes WHERE path='topics/bad.md'").fetchone()
    assert row["type"] == "invalid"
    assert app.db.execute("SELECT 1 FROM notes WHERE path='topics/good.md'").fetchone()


def test_stale_detects_external_edits(app):
    small_vault(app)
    app.index.rebuild()
    assert app.index.stale() is False
    (app.vault.root / "devices" / "ender-3.md").write_text("changed by hand\n", encoding="utf-8")
    assert app.index.stale() is True


def test_update_reindexes_one_note_and_resolves_new_links(app):
    small_vault(app)
    app.index.rebuild()
    assert app.index.dangling()[0]["target"] == "missing-note"

    app.vault.write("topics/missing-note.md", "now it exists", op="create")  # write() calls update()
    assert app.index.dangling() == []
    assert "topics/missing-note.md" in app.index.links("devices/ender-3.md", "out")
    assert app.index.links("topics/missing-note.md", "in") == ["devices/ender-3.md"]


def test_update_removes_a_deleted_note(app):
    small_vault(app)
    app.index.rebuild()
    app.vault.delete("projects/music-bot.md")
    assert app.db.execute("SELECT 1 FROM notes WHERE path='projects/music-bot.md'").fetchone() is None
    assert app.db.execute("SELECT 1 FROM aliases WHERE path='projects/music-bot.md'").fetchone() is None


def test_fts_query_sanitises():
    assert fts_query("ender 3 printer") == '"ender" AND "3" AND "printer"'
    assert fts_query('bad " quote', "OR") == '"bad" OR "quote"'
    assert fts_query("  ") == ""


def test_fts_search_finds_words(app):
    small_vault(app)
    app.index.rebuild()
    hits = app.index.search_fts("discord bot")
    assert hits and hits[0]["path"] == "projects/music-bot.md"
    assert hits[0]["snippet"]
    assert app.index.search_fts("discord bot", type="device") == []


async def test_search_fuses_fts_and_cosine(app, fake):
    small_vault(app)
    await app.index.a_rebuild()
    assert len(app.index.vectors) == len(app.vault.list())

    hits = await app.index.search("printer nozzle heating", k=5)
    paths = [h["path"] for h in hits]
    assert "procedures/pla-print/SKILL.md" in paths
    assert all(h["score"] > 0 and "path" in h and "title" in h for h in hits)


async def test_cosine_ranks_a_near_duplicate_first(app):
    app.vault.write("topics/a.md", "octoprint controls the ender 3 printer over the lan", op="create")
    app.vault.write("topics/b.md", "sourdough bread needs a hot oven and patience", op="create")
    await app.index.a_rebuild()

    vector = await app.index.embed_query("octoprint controls the ender 3 printer over the lan")
    ranked = app.index.nearest(vector, 2)
    assert ranked[0][0] == "topics/a.md"
    assert ranked[0][1] > ranked[1][1]
    assert cosine(array("f", [1.0, 0.0]), array("f", [1.0, 0.0])) == pytest.approx(1.0)


async def test_embeddings_are_stored_and_reused_on_rebuild(app, fake):
    app.vault.write("topics/a.md", "one", op="create")
    await app.index.a_rebuild()
    calls = len(fake.embed_calls)
    blob = app.db.execute("SELECT embedding FROM notes WHERE path='topics/a.md'").fetchone()["embedding"]
    assert blob is not None

    result = await app.index.a_rebuild()  # nothing changed → no new embedding calls
    assert len(fake.embed_calls) == calls
    assert result["embedded"] >= 1


def _add_embed_preset(app, name: str, model: str) -> None:
    app.db.execute(
        "INSERT INTO presets(name, provider, model, params, enabled, created_at, updated_at) "
        "VALUES (?, 'local-ollama', ?, '{}', 1, '', '')",
        (name, model),
    )


async def _settle(app) -> None:
    await app.index.embed_pending()
    while app.index._draining:
        await asyncio.sleep(0)


async def test_vectors_record_their_model(app, fake):
    app.vault.write("topics/a.md", "one", op="create")
    await app.index.a_rebuild()
    row = app.db.execute("SELECT embed_model FROM notes WHERE path='topics/a.md'").fetchone()
    assert row["embed_model"] == "nomic-embed-text"
    assert app.index.vectors_model == "nomic-embed-text"


async def test_changing_the_embedding_model_reembeds_everything(app, fake):
    small_vault(app)
    await app.index.a_rebuild()
    total = len(app.vault.list())
    assert len(app.index.vectors) == total
    _add_embed_preset(app, "other-embed", "qwen3-embedding")
    app.db.execute("UPDATE model_roles SET preset='other-embed' WHERE role='embedding'")
    calls = len(fake.embed_calls)
    app.bus.emit("models.changed", {"kind": "role", "name": "embedding"})  # what the route emits
    assert app.index.vectors_model == "qwen3-embedding"
    await _settle(app)
    assert len(fake.embed_calls) > calls
    models = {r["embed_model"] for r in app.db.execute("SELECT embed_model FROM notes")}
    assert models == {"qwen3-embedding"}
    assert len(app.index.vectors) == total


async def test_a_query_notices_a_model_change_without_an_event(app, fake):
    small_vault(app)
    await app.index.a_rebuild()
    _add_embed_preset(app, "other-embed", "qwen3-embedding")
    app.db.execute("UPDATE model_roles SET preset='other-embed' WHERE role='embedding'")
    # no event: the stale vectors must not be compared with the new model's query
    assert await app.index.embed_query("printer") is None
    assert app.index.vectors == {} and app.index.vectors_model == "qwen3-embedding"
    await _settle(app)
    assert await app.index.embed_query("printer") is not None


async def test_vectors_without_a_model_are_reembedded_once(app, fake):
    app.vault.write("topics/a.md", "one", op="create")
    await app.index.a_rebuild()
    app.db.execute("UPDATE notes SET embed_model=NULL")  # rows written before 0009
    assert app.index.load() == 0
    await _settle(app)
    assert app.db.execute("SELECT embed_model FROM notes WHERE path='topics/a.md'").fetchone()[0] == "nomic-embed-text"
    calls = len(fake.embed_calls)
    await app.index.a_rebuild()
    assert len(fake.embed_calls) == calls


async def test_a_drain_follows_a_model_change(app, fake):
    """Vectors are tied to their model, so a drain that finishes after the role moved
    on does not leak the old model's vectors into memory."""
    app.vault.write("topics/a.md", "one", op="create")
    app.index.rebuild()
    model = app.index.vectors_model
    _add_embed_preset(app, "other-embed", "qwen3-embedding")
    app.db.execute("UPDATE model_roles SET preset='other-embed' WHERE role='embedding'")
    await _settle(app)  # the drain notices the change before its first batch
    assert model != app.index.vectors_model == "qwen3-embedding"
    assert {r["embed_model"] for r in app.db.execute("SELECT embed_model FROM notes")} == {"qwen3-embedding"}


async def test_index_works_with_embeddings_disabled(app):
    app.settings.set(app.db, "models.embeddings_enabled", False)
    small_vault(app)
    result = await app.index.a_rebuild()
    assert result["embedded"] == 0
    assert app.index.vectors == {}
    hits = await app.index.search("discord bot")
    assert [h["path"] for h in hits] == ["projects/music-bot.md"]


async def test_index_survives_an_embedding_failure(app, fake):
    fake.embeddings_available = False
    small_vault(app)
    result = await app.index.a_rebuild()
    assert result["notes"] > 0 and result["embedded"] == 0
    assert (await app.index.search("discord bot"))[0]["path"] == "projects/music-bot.md"


def test_core_blocks_truncate_visibly(app):
    app.vault.put_raw("core/about-me.md", "---\ntitle: About me\n---\n" + ("x" * 50))
    blocks = dict(app.index.core_blocks(cap_chars=20))
    body = blocks["core/about-me.md"]
    assert body.startswith("x" * 20)
    assert "[truncated: 20 of 50 chars — edit core/about-me.md to shorten]" in body

    fill = {row["path"]: row for row in app.index.core_fill(cap_chars=20)}
    assert fill["core/about-me.md"]["chars"] == 50 and fill["core/about-me.md"]["over"] is True


def test_core_blocks_are_ordered_by_filename(app):
    app.index.rebuild()
    assert [p for p, _ in app.index.core_blocks()] == [
        "core/about-me.md",
        "core/conventions.md",
        "core/preferences.md",
    ]


def test_procedures_index_lists_approval_state(app):
    small_vault(app)
    target = app.vault.root / "procedures" / "third-party" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\nname: third-party\ndescription: from outside\n---\nbody\n", encoding="utf-8")
    app.index.rebuild()
    lines = app.index.procedures_index().splitlines()
    assert "pla-print — Print PLA on a printer" in lines
    assert "third-party — from outside (awaiting approval)" in lines


def test_graph_and_links(app):
    small_vault(app)
    app.index.rebuild()
    graph = app.index.graph(center="devices/ender-3.md", depth=1)
    paths = {n["path"] for n in graph["nodes"]}
    assert {"devices/ender-3.md", "procedures/pla-print/SKILL.md", "projects/music-bot.md"} <= paths
    assert {"src": "projects/music-bot.md", "dst": "devices/ender-3.md"} in graph["edges"]
    with pytest.raises(ValueError):
        app.index.links("devices/ender-3.md", "sideways")


def test_transcript_search_and_neighbours(app):
    from loom import db

    conv = app.desk_id()
    for text in ("the bed is 220 by 220", "unrelated chatter", "and the nozzle is 0.4"):
        app.db.execute(
            "INSERT INTO events(conversation_id, kind, content, ts) VALUES (?, 'user', ?, ?)",
            (conv, text, db.now_iso()),
        )
    hits = app.index.transcript_search("nozzle")
    assert len(hits) == 1
    assert hits[0]["kind"] == "user" and hits[0]["conversation_id"] == conv
    assert "nozzle" in hits[0]["snippet"]

    around = app.index.neighbours(hits[0]["event_id"], 2)
    assert [a["content"] for a in around][-1] == "and the nozzle is 0.4"
    assert app.index.transcript_search("nozzle", conversation_id="nope") == []


def test_tool_calls_are_searchable_and_0007_backfills_them(app, monkeypatch):
    """A command that was run is findable (2026-10-12): new tool_call events are indexed
    by the trigger, and 0007 backfills the ones written before it."""
    from loom import db
    from loom.conversation import events

    conv = app.desk_id()
    call = [{"id": "c1", "name": "shell", "arguments": {"command": "orca-slicer --export-gcode cube.3mf"}}]
    live = events.append(app, conv, "tool_call", db.dumps(call))
    hits = app.index.transcript_search("orca-slicer export-gcode")
    assert [h["event_id"] for h in hits] == [live] and hits[0]["kind"] == "tool_call"

    # a database from before 0007: the old trigger, and a tool call it never indexed
    app.db.execute("DROP TRIGGER events_fts_ai")
    app.db.execute(
        "CREATE TRIGGER events_fts_ai AFTER INSERT ON events WHEN new.kind IN ('user', 'assistant', 'tool_result') "
        "BEGIN INSERT INTO events_fts(rowid, content) VALUES (new.id, new.content); END"
    )
    app.db.execute("INSERT INTO events_fts(events_fts) VALUES ('delete-all')")
    old = events.append(app, conv, "tool_call", db.dumps([{"id": "c2", "name": "shell", "arguments": {"command": "prusa-slicer --slice"}}]))
    assert app.index.transcript_search("prusa-slicer") == []
    app.db.execute("PRAGMA user_version=6")
    # only 0007 is replayed: later migrations are not re-runnable over their own result
    only_0007 = [(n, p) for n, p in db.list_migrations() if n <= 7]
    monkeypatch.setattr(db, "list_migrations", lambda: only_0007)

    assert db.migrate(app.db) == [7]
    assert [h["event_id"] for h in app.index.transcript_search("prusa-slicer")] == [old]
    assert [h["event_id"] for h in app.index.transcript_search("orca-slicer")] == [live]
    newer = events.append(app, conv, "tool_call", db.dumps([{"id": "c3", "name": "shell", "arguments": {"command": "cura-engine slice"}}]))
    assert [h["event_id"] for h in app.index.transcript_search("cura-engine")] == [newer]
