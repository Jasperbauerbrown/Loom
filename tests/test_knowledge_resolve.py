"""Resolution and pre-load: slot [9] (03 §8.2–8.3)."""

from __future__ import annotations

from loom.knowledge import resolve


def vault_with_printer(app) -> None:
    app.vault.write(
        "devices/ender-3.md",
        "A 3D printer. Heat it with [[pla-print]]; it lives on the [[projects/music-bot]] host.",
        op="create",
        frontmatter={"aliases": ["ender 3", "the printer", "printer"]},
    )
    app.vault.write(
        "procedures/pla-print/SKILL.md",
        "Heat the bed to 60 and the nozzle to 200.",
        op="create",
        frontmatter={"name": "pla-print", "description": "Print PLA"},
    )
    app.vault.write("projects/music-bot.md", "A discord bot.", op="create", frontmatter={"aliases": ["music bot"]})
    app.index.rebuild()


async def test_alias_resolution_finds_the_device(app):
    vault_with_printer(app)
    assert await app.index.resolve("heat up the ender 3") == ["devices/ender-3.md"]
    assert await app.index.resolve("is the printer on?") == ["devices/ender-3.md"]
    assert await app.index.resolve("check the MUSIC BOT logs") == ["projects/music-bot.md"]


async def test_longest_alias_match_wins_per_span(app):
    app.vault.write("devices/ender-3.md", "the printer", op="create", frontmatter={"aliases": ["printer"]})
    app.vault.write(
        "devices/resin-printer.md", "the other one", op="create", frontmatter={"aliases": ["resin printer"]}
    )
    app.index.rebuild()
    assert await app.index.resolve("start the resin printer") == ["devices/resin-printer.md"]
    assert await app.index.resolve("start the printer") == ["devices/ender-3.md"]


async def test_core_notes_are_never_resolved(app):
    vault_with_printer(app)
    app.vault.put_raw("core/about-me.md", "---\ntitle: About me\naliases: [about me]\n---\nI own an ender 3.\n")
    paths = await app.index.resolve("about me")
    assert not any(p.startswith("core/") for p in paths)


async def test_embeddings_fill_up_to_the_cap(app):
    app.vault.write("topics/octoprint.md", "octoprint controls printers over the network", op="create")
    await app.index.a_rebuild()
    paths = await app.index.resolve("octoprint controls printers over the network", threshold=0.2)
    assert "topics/octoprint.md" in paths


async def test_resolve_respects_exclude(app):
    vault_with_printer(app)
    assert await app.index.resolve("the ender 3", exclude={"devices/ender-3.md"}) == []


def test_preload_puts_the_linked_procedure_first(app):
    vault_with_printer(app)
    text = app.index.preload(["devices/ender-3.md"])
    assert text.startswith("### devices/ender-3.md")
    assert text.index("### procedures/pla-print/SKILL.md") < text.index("### projects/music-bot.md")
    assert "Heat the bed to 60" in text


def test_preload_never_includes_core_and_never_repeats(app):
    vault_with_printer(app)
    text = app.index.preload(["devices/ender-3.md", "devices/ender-3.md", "projects/music-bot.md"])
    assert "core/" not in text
    assert text.count("### devices/ender-3.md") == 1
    assert text.count("### projects/music-bot.md") == 1


def test_preload_loads_every_matched_note_before_any_link(app):
    """A link-heavy first match must not starve the second match (2026-09-30): both
    matched bodies land, the first note's links get stubs."""
    vault_with_printer(app)
    both = (
        len(app.index._render("devices/ender-3.md")) + len(app.index._render("projects/music-bot.md"))
    ) // 4 + 1
    text = app.index.preload(["devices/ender-3.md", "projects/music-bot.md"], budget_tokens=both)
    assert "### devices/ender-3.md" in text
    assert "### projects/music-bot.md" in text
    assert "### procedures/pla-print/SKILL.md" not in text
    assert "- procedures/pla-print/SKILL.md (not loaded; knowledge_read for more)" in text
    assert text.index("### projects/music-bot.md") < text.index("- procedures/pla-print/SKILL.md")


def test_preload_budget_names_what_did_not_fit(app):
    vault_with_printer(app)
    first_only = len(app.index._render("devices/ender-3.md")) // 4 + 1
    text = app.index.preload(["devices/ender-3.md"], budget_tokens=first_only)
    assert "### devices/ender-3.md" in text
    assert "### procedures/pla-print/SKILL.md" not in text
    assert "- procedures/pla-print/SKILL.md (not loaded; knowledge_read for more)" in text


def test_preload_lists_everything_when_the_budget_is_tiny(app):
    vault_with_printer(app)
    text = app.index.preload(["devices/ender-3.md"], budget_tokens=1)
    assert text.startswith("- devices/ender-3.md (not loaded")
    assert "###" not in text


async def test_knowledge_block_wraps_and_degrades(app):
    vault_with_printer(app)
    block = await resolve.knowledge_block(app, "heat up the ender 3")
    assert block.startswith(resolve.OPEN) and block.endswith(resolve.CLOSE)
    assert "### devices/ender-3.md" in block
    assert await resolve.knowledge_block(app, "   ") == ""
    assert await resolve.knowledge_block(app, "nothing here matches xyzzy") == ""
