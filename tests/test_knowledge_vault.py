"""The vault: bootstrap, the four write ops, stamping, guards, git, procedures."""

from __future__ import annotations

import pytest

from loom.knowledge import notes
from loom.knowledge.vault import VaultError


def test_bootstrap_is_idempotent_and_committed(app):
    vault = app.vault
    for path in ("core/about-me.md", "core/preferences.md", "core/conventions.md"):
        assert vault.exists(path)
    assert (vault.root / ".git").is_dir()
    assert (vault.root / "devices").is_dir() and (vault.root / "procedures").is_dir()
    assert (vault.root / ".gitignore").read_text(encoding="utf-8").find("workspace") > 0

    log = vault.git.log()
    # Packages may add their own notes after the bootstrap (the self-modification
    # procedure, 03 §15.1), so the bootstrap commit is the oldest, not the newest.
    assert log and log[-1]["message"] == "loom: bootstrap vault"

    before = vault.read("core/about-me.md").raw
    vault.bootstrap()  # second run changes nothing
    assert vault.read("core/about-me.md").raw == before
    assert len(vault.git.log()) == len(log)


def test_create_stamps_frontmatter_and_commits(app):
    note = app.vault.write(
        "devices/ender-3.md",
        "A 3D printer in the workshop.",
        op="create",
        frontmatter={"aliases": ["Ender 3", "The Printer", "ender 3"], "tags": ["lan"]},
    )
    assert note.title == "Ender 3"
    assert note.type == "device"
    assert note.aliases == ["ender 3", "the printer"]  # lower-cased, de-duplicated
    assert note.frontmatter["modified"].endswith("Z")
    assert app.vault.git.log("devices/ender-3.md")[0]["message"] == "loom: create devices/ender-3.md"


def test_create_refuses_existing(app):
    app.vault.write("topics/x.md", "one", op="create")
    with pytest.raises(VaultError) as exc:
        app.vault.write("topics/x.md", "two", op="create")
    assert "already exists" in str(exc.value)


def test_append_and_replace(app):
    app.vault.write("topics/x.md", "first line", op="create")
    note = app.vault.write("topics/x.md", "second line", op="append")
    assert note.body.strip().splitlines() == ["first line", "", "second line"]

    note = app.vault.write("topics/x.md", "SECOND", op="replace", old="second line")
    assert "SECOND" in note.body and "second line" not in note.body


def test_replace_requires_a_unique_old(app):
    app.vault.write("topics/x.md", "dup\ndup\n", op="create")
    with pytest.raises(VaultError) as exc:
        app.vault.write("topics/x.md", "one", op="replace", old="dup")
    assert "appears 2 times" in str(exc.value)
    with pytest.raises(VaultError) as exc:
        app.vault.write("topics/x.md", "one", op="replace", old="nope")
    assert "not found" in str(exc.value)


def test_rethink_requires_reason_and_posts_an_item(app):
    app.vault.write("topics/x.md", "old shape", op="create")
    with pytest.raises(VaultError):
        app.vault.write("topics/x.md", "new shape", op="rethink")

    note = app.vault.write("topics/x.md", "new shape", op="rethink", reason="the old note mixed two entities")
    assert note.body.strip() == "new shape"
    rows = [r for r in app.db.execute("SELECT * FROM items WHERE kind='knowledge'")]
    assert len(rows) == 1
    assert rows[0]["title"] == "Rewrote topics/x.md"
    assert "two entities" in rows[0]["body"]


def test_relative_dates_are_refused(app):
    with pytest.raises(VaultError) as exc:
        app.vault.write("topics/x.md", "Printed it yesterday.", op="create")
    assert str(exc.value) == "use absolute dates: found 'yesterday'"
    assert not app.vault.exists("topics/x.md")


def test_path_traversal_and_absolute_paths_are_refused(app):
    for bad in ("../outside.md", "topics/../../x.md", "/etc/passwd", "C:/Windows/system.ini"):
        with pytest.raises(VaultError):
            app.vault.write(bad, "x", op="create")


def test_write_emits_knowledge_changed(app):
    seen = []
    app.bus.listen(lambda e: seen.append(e) if e["type"] == "knowledge.changed" else None)
    app.vault.write("topics/x.md", "body", op="create")
    assert seen and seen[-1]["data"]["paths"] == ["topics/x.md"]


def test_put_raw_commits_as_user_and_restamps(app):
    app.vault.write("topics/x.md", "body", op="create")
    note = app.vault.put_raw("topics/x.md", "---\ntitle: Renamed\n---\nedited by hand\n")
    assert note.title == "Renamed"
    assert note.frontmatter["modified"].endswith("Z")
    assert app.vault.git.log("topics/x.md")[0]["message"] == "user: edit topics/x.md"


def test_put_raw_keeps_a_malformed_note(app):
    app.vault.write("topics/x.md", "body", op="create")
    note = app.vault.put_raw("topics/x.md", "---\ntitle: [oops\n---\nstill mine\n")
    assert note.error is not None and note.type == "invalid"
    assert "still mine" in note.raw


def test_delete(app):
    app.vault.write("topics/x.md", "body", op="create")
    app.vault.delete("topics/x.md")
    assert not app.vault.exists("topics/x.md")
    assert app.vault.git.log("topics/x.md")[0]["message"] == "user: delete topics/x.md"


def test_procedures_approval_flow(app):
    # A procedure Loom writes itself is approved at write time.
    app.vault.write(
        "procedures/pla-print/SKILL.md",
        "1. Heat the bed.\n",
        op="create",
        frontmatter={"name": "pla-print", "description": "Print PLA on the Ender 3"},
    )
    mine = app.vault.find_procedure("pla-print")
    assert mine["approved"] is True
    assert app.vault.load_procedure("pla-print").startswith("1. Heat")

    # One copied in from outside is not, until the user approves it.
    target = app.vault.root / "procedures" / "third-party" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\nname: third-party\ndescription: from the internet\n---\nrm -rf\n", encoding="utf-8")
    row = app.vault.find_procedure("third-party")
    assert row["approved"] is False
    with pytest.raises(VaultError) as exc:
        app.vault.load_procedure("third-party")
    assert "not approved" in str(exc.value)

    app.vault.approve_procedure("third-party")
    assert app.vault.find_procedure("third-party")["approved"] is True
    assert "rm -rf" in app.vault.load_procedure("third-party")


def test_unknown_procedure_names_the_known_ones(app):
    with pytest.raises(VaultError) as exc:
        app.vault.load_procedure("nope")
    assert "no procedure named" in str(exc.value)


def test_list_and_tree(app):
    app.vault.write("devices/ender-3.md", "x", op="create")
    paths = app.vault.list()
    assert "core/about-me.md" in paths and "devices/ender-3.md" in paths
    assert app.vault.list(prefix="devices/") == ["devices/ender-3.md"]

    tree = app.vault.tree()
    devices = [c for c in tree["children"] if c["name"] == "devices"][0]
    assert devices["type"] == "dir"
    assert [c["path"] for c in devices["children"]] == ["devices/ender-3.md"]


def test_unknown_op(app):
    with pytest.raises(VaultError) as exc:
        app.vault.write("topics/x.md", "b", op="overwrite")
    assert "unknown op" in str(exc.value)


def test_invalid_note_reads_as_invalid_and_never_raises(app):
    (app.vault.root / "topics" / "bad.md").write_text("---\na: [1\n---\nbody\n", encoding="utf-8")
    note = app.vault.read("topics/bad.md")
    assert note.type == "invalid" and note.error
    assert notes.parse(note.raw).error


# -- the knowledge tools (03 §8.4), driven exactly as the dispatcher drives them ------
def _ctx(app):
    from loom.tools.registry import ToolContext

    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="call_1")


def test_knowledge_tools_are_registered(app):
    for name in (
        "knowledge_search",
        "knowledge_read",
        "knowledge_write",
        "knowledge_links",
        "procedure_load",
        "transcript_search",
    ):
        tool = app.tools.get(name)
        assert tool is not None and tool.source == "native"
        assert tool.description.strip() and tool.path_params == ()
    assert app.tools.get("knowledge_write").read_only is False
    assert app.tools.get("knowledge_read").read_only is True


async def test_knowledge_write_and_read_tools(app):
    ctx = _ctx(app)
    write = app.tools.get("knowledge_write").handler
    result = await write(ctx, {"path": "devices/ender-3.md", "op": "create", "content": "A printer.",
                               "frontmatter": {"aliases": ["the printer"]}})
    assert result.ok and result.content == "created devices/ender-3.md"

    result = await write(ctx, {"path": "devices/ender-3.md", "op": "append", "content": "Bed is 220x220."})
    assert result.content == "appended to devices/ender-3.md"

    read = app.tools.get("knowledge_read").handler
    result = await read(ctx, {"path": "devices/ender-3.md"})
    assert "Bed is 220x220." in result.content and "aliases" in result.content

    sliced = await read(ctx, {"path": "devices/ender-3.md", "offset": 0, "limit": 2})
    assert "knowledge_read(path, offset=2) for more" in sliced.content


async def test_knowledge_write_tool_returns_errors_verbatim(app):
    ctx = _ctx(app)
    write = app.tools.get("knowledge_write").handler
    result = await write(ctx, {"path": "topics/x.md", "op": "create", "content": "I did it yesterday."})
    assert not result.ok and result.content == "error: use absolute dates: found 'yesterday'"

    result = await write(ctx, {"path": "../escape.md", "op": "create", "content": "x"})
    assert not result.ok and "inside the vault" in result.content

    result = await write(ctx, {"path": "topics/x.md", "op": "append", "content": "x"})
    assert not result.ok and "does not exist; use create" in result.content

    await write(ctx, {"path": "topics/x.md", "op": "create", "content": "body"})
    result = await write(ctx, {"path": "topics/x.md", "op": "replace", "content": "x"})
    assert not result.ok and "replace requires" in result.content

    result = await write(ctx, {"path": "topics/x.md", "op": "rethink", "content": "x"})
    assert not result.ok and "rethink requires a reason" in result.content


async def test_knowledge_search_and_links_tools(app):
    ctx = _ctx(app)
    app.vault.write("devices/ender-3.md", "A printer. See [[pla-print]].", op="create")
    app.vault.write("procedures/pla-print/SKILL.md", "Heat the bed.", op="create",
                    frontmatter={"name": "pla-print", "description": "Print PLA"})
    app.index.rebuild()

    found = await app.tools.get("knowledge_search").handler(ctx, {"query": "printer"})
    assert "devices/ender-3.md" in found.content

    links = app.tools.get("knowledge_links").handler
    assert "procedures/pla-print/SKILL.md" in (await links(ctx, {"path": "devices/ender-3.md", "direction": "out"})).content
    assert "devices/ender-3.md" in (await links(ctx, {"path": "procedures/pla-print/SKILL.md", "direction": "in"})).content
    assert not (await links(ctx, {"path": "devices/ender-3.md", "direction": "sideways"})).ok

    loaded = await app.tools.get("procedure_load").handler(ctx, {"name": "pla-print"})
    assert loaded.content == "Heat the bed."


async def test_transcript_search_tool(app):
    from loom import db

    ctx = _ctx(app)
    app.db.execute(
        "INSERT INTO events(conversation_id, kind, content, ts) VALUES (?, 'user', ?, ?)",
        (app.desk_id(), "the nozzle is 0.4mm", db.now_iso()),
    )
    result = await app.tools.get("transcript_search").handler(ctx, {"query": "nozzle", "neighbours": True})
    assert "nozzle" in result.content and result.meta["count"] == 1
    assert (await app.tools.get("transcript_search").handler(ctx, {"query": "zzzz"})).meta["count"] == 0


def test_write_folds_a_frontmatter_block_found_in_the_body(app):
    """The model copies the shape it reads back; a leading block in `content` merges into
    the frontmatter instead of nesting (two live notes had two blocks, 2026-09-21)."""
    text = "---\ntitle: 3D Printers\naliases: [3d printers]\nmodified: '2026-01-01T00:00:00Z'\n---\n\n# 3D Printers\n\nBody."
    note = app.vault.write("devices/printers.md", text, op="create", frontmatter={"tags": ["lan"]})
    assert note.body.strip().startswith("# 3D Printers") and "---" not in note.body
    assert note.title == "3D Printers" and note.aliases == ["3d printers"] and note.frontmatter["tags"] == ["lan"]
    assert note.frontmatter["modified"] != "2026-01-01T00:00:00Z"
    assert note.raw.count("\n---\n") == 1  # one closing fence, i.e. one block

    # the explicit argument wins over the embedded block; rethink folds the same way
    note = app.vault.write(
        "devices/printers.md", "---\ntitle: Printers\n---\n# Printers\n", op="rethink", reason="shape", frontmatter={"title": "All printers"}
    )
    assert note.title == "All printers" and note.body.strip() == "# Printers"


async def test_knowledge_read_over_the_cap_returns_one_page_and_is_never_spilled(app):
    """knowledge_read pages on `conversation.tool_output_cap` and is `paged`: a fixed 8000
    here was the off-by-footer trap that looped artifact_recall (2026-09-22). Pages end
    on whole lines, so the next page opens with the cut line intact."""
    from loom.tools import dispatch
    from loom.tools.registry import ToolContext

    app.settings.set(app.db, "conversation.tool_output_cap", 500)
    line_len = len("line 0000 " + "-" * 12)
    app.vault.write("topics/big.md", "\n".join(f"line {i:04d} {'-' * 12}" for i in range(200)), op="create")
    rows = app.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    tool = app.tools.get("knowledge_read")
    assert tool.paged
    ctx = ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1", event_id=2)

    first = await dispatch.execute(app, ctx, tool, {"path": "topics/big.md"})
    assert first.ok and "artifact_id" not in first.meta
    page, footer = first.content.rsplit("\n", 1)
    assert len(page) <= 500 and page.splitlines()[-1].endswith("-" * 12)
    assert footer.startswith("[lines 1–") and "knowledge_read(path, offset=" in footer
    assert app.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == rows

    end = int(footer.split("offset=")[1].split(")")[0])
    second = await dispatch.execute(app, ctx, tool, {"path": "topics/big.md", "offset": end})
    assert second.ok and len(second.content.splitlines()[0]) == line_len
    assert app.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == rows
    # pages cover the note with nothing skipped: the first body line after the frontmatter
    # is line 0000 and the second page starts exactly where the first stopped
    seen = page.splitlines() + second.content.rsplit("\n", 1)[0].splitlines()
    body_lines = [l for l in seen if l.startswith("line ")]
    assert [int(l.split()[1]) for l in body_lines] == list(range(len(body_lines)))


async def test_knowledge_read_pin_and_unpin(app):
    """Pinning is `knowledge_read(pin=…)` (03 §6.7, 2026-09-22): the note joins the
    conversation's pinned list and is shown in its context every turn; `pin=false`
    drops it. Any conversation kind may pin. A failed read pins nothing, and the handler
    re-reads the row so two pins in one turn both stick."""
    from loom.conversation import pins
    from loom.tools import dispatch
    from loom.tools.registry import ToolContext

    app.vault.write("projects/bot.md", "A music bot.", op="create")
    app.vault.write("devices/pi.md", "A Pi.", op="create")
    tool = app.tools.get("knowledge_read")
    desk = app.desk_id()

    def ctx() -> ToolContext:
        return ToolContext(app=app, conversation=app.conversation(desk), call_id="c1", event_id=1)

    def pinned() -> list[str]:
        return pins.get(app.conversation(desk))

    r = await dispatch.execute(app, ctx(), tool, {"path": "projects/bot.md"})
    assert r.ok and "[pinned" not in r.content and pinned() == []

    r = await dispatch.execute(app, ctx(), tool, {"path": "projects/bot.md", "pin": True})
    assert r.ok and r.content.rstrip().endswith("to drop it]") and pinned() == ["projects/bot.md"]

    stale = ctx()  # one row object for two calls: the second pin must not drop the first
    r = await dispatch.execute(app, stale, tool, {"path": "projects/bot.md", "pin": True})
    assert r.ok and pinned() == ["projects/bot.md"]
    r = await dispatch.execute(app, stale, tool, {"path": "devices/pi.md", "pin": True})
    assert r.ok and pinned() == ["projects/bot.md", "devices/pi.md"]

    r = await dispatch.execute(app, ctx(), tool, {"path": "projects/bot.md", "pin": False})
    assert r.ok and r.content.rstrip().endswith("[unpinned]") and pinned() == ["devices/pi.md"]
    r = await dispatch.execute(app, ctx(), tool, {"path": "projects/bot.md", "pin": False})
    assert r.ok and pinned() == ["devices/pi.md"]  # not pinned: a no-op, not an error

    r = await dispatch.execute(app, ctx(), tool, {"path": "projects/missing.md", "pin": True})
    assert not r.ok and pinned() == ["devices/pi.md"]
