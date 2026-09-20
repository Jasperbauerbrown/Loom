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


def test_status_render_parse_roundtrip():
    fields = {"state": "working", "goal": "Ship it", "done": "a\nb", "next": "c"}
    block = notes.render_status("01J8", fields)
    assert block.startswith(notes.STATUS_BEGIN % "01J8")
    assert block.endswith(notes.STATUS_END)
    assert "  - a" in block and "  - b" in block
    back, owner = notes.parse_status(f"## Status\n{block}\n")
    assert owner == "01J8"
    assert back == {"state": "working", "goal": "Ship it", "done": "a\nb", "next": "c"}


def test_status_fields_render_in_fixed_order():
    block = notes.render_status("o", {"next": "n", "state": "working", "goal": "g"})
    assert block.index("state:") < block.index("goal:") < block.index("next:")


def test_replace_status_in_place():
    body = "Intro\n\n## Status\n" + notes.render_status("o1", {"state": "planning"}) + "\n\nTail\n"
    out = notes.replace_status(body, "o1", {"state": "working"})
    assert "state: working" in out and "state: planning" not in out
    assert out.count(notes.STATUS_END) == 1
    assert out.startswith("Intro") and out.rstrip().endswith("Tail")


def test_replace_status_appends_section_when_missing():
    out = notes.replace_status("Just a body.\n", "o2", {"state": "planning"})
    assert "## Status" in out
    fields, owner = notes.parse_status(out)
    assert owner == "o2" and fields == {"state": "planning"}


def test_replace_status_uses_existing_heading():
    out = notes.replace_status("Body\n\n## Status\n\nnothing yet\n", "o3", {"state": "done"})
    assert out.count("## Status") == 1
    fields, _ = notes.parse_status(out)
    assert fields == {"state": "done"}


def test_parse_status_absent():
    assert notes.parse_status("no block here") == (None, None)


def test_titles_and_slugs():
    assert notes.title_from_path("devices/ender-3.md") == "Ender 3"
    assert notes.title_from_path("procedures/pla-print/SKILL.md") == "Pla print"
    assert notes.slug("Ender 3 — Control!") == "ender-3-control"
    assert notes.slug("") == "untitled"
