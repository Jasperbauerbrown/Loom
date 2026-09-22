"""Pure note parsing: frontmatter, wikilinks, aliases, status blocks, relative dates."""

from __future__ import annotations

from loom.knowledge import notes


def test_parse_roundtrip():
    text = "---\ntitle: Ender 3\naliases: [ender 3, the printer]\n---\nBody line.\n"
    parsed = notes.parse(text)
    assert parsed.error is None
    assert parsed.frontmatter["title"] == "Ender 3"
    assert parsed.frontmatter["aliases"] == ["ender 3", "the printer"]
    assert parsed.body == "Body line.\n"
    again = notes.parse(notes.emit(parsed.frontmatter, parsed.body))
    assert again.frontmatter == parsed.frontmatter
    assert again.body.strip() == "Body line."


def test_emit_keeps_key_order_and_inlines_short_lists():
    out = notes.emit({"title": "A", "aliases": ["x", "y"], "type": "device"}, "Body")
    assert out.index("title:") < out.index("aliases:") < out.index("type:")
    assert "aliases: [x, y]" in out


def test_parse_without_frontmatter_is_not_an_error():
    parsed = notes.parse("Just a body.\n")
    assert parsed.frontmatter == {} and parsed.error is None
    assert parsed.body == "Just a body.\n"


def test_malformed_frontmatter_keeps_whole_text():
    text = "---\ntitle: [unclosed\n---\nBody.\n"
    parsed = notes.parse(text)
    assert parsed.error is not None
    assert parsed.frontmatter == {}
    assert parsed.body == text


def test_non_mapping_frontmatter_is_an_error():
    parsed = notes.parse("---\n- a\n- b\n---\nBody\n")
    assert parsed.error and "mapping" in parsed.error


def test_wikilinks_strip_heading_and_alias():
    body = "See [[ender-3]], [[ender-3#Bed|the bed]] and [[people/sam|Sam]]. Not a [link](x)."
    assert notes.wikilinks(body) == ["ender-3", "ender-3", "people/sam"]


def test_normalise_alias():
    assert notes.normalise_alias("  The   Printer! ") == "the printer"
    assert notes.normalise_alias("Ender-3") == "ender-3"
    assert notes.normalise_alias("node.js") == "node.js"
    assert notes.normalise_alias("(ender_3)") == "ender_3"


def test_relative_dates_positive_and_negative():
    for phrase in ("yesterday", "Tomorrow", "last week", "next month", "this year", "3 days ago", "in 2 weeks"):
        assert notes.has_relative_dates(f"Printed {phrase} on the bed.") is not None
    for phrase in ("2026-09-20", "on 20 September 2026", "todays-notes.md", "yesterdays"):
        assert notes.has_relative_dates(f"Printed {phrase}.") is None


def test_titles_and_slugs():
    assert notes.title_from_path("devices/ender-3.md") == "Ender 3"
    assert notes.title_from_path("procedures/pla-print/SKILL.md") == "Pla print"
    assert notes.slug("Ender 3 — Control!") == "ender-3-control"
    assert notes.slug("") == "untitled"
