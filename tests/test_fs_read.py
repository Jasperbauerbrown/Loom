"""fs_read: line numbering and paging (parity audit #1)."""

from __future__ import annotations

import pytest

from loom.tools import fs_guard
from loom.tools.registry import ToolContext


@pytest.fixture
def project(app, tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    fs_guard.add_root(app, str(root), added_by="test")
    return root


@pytest.fixture
def ctx(app, project):
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1", event_id=1)


async def run(app, ctx, name, args):
    return await app.tools.get(name).handler(ctx, args)


async def test_line_numbers_on_by_default(app, ctx, project):
    (project / "a.txt").write_text("one\ntwo\nthree", encoding="utf-8")
    result = await run(app, ctx, "fs_read", {"path": str(project / "a.txt")})
    assert result.ok
    assert result.content == "1\tone\n2\ttwo\n3\tthree"


async def test_line_numbers_can_be_turned_off(app, ctx, project):
    (project / "a.txt").write_text("one\ntwo\nthree", encoding="utf-8")
    result = await run(app, ctx, "fs_read", {"path": str(project / "a.txt"), "line_numbers": False})
    assert result.ok
    assert result.content == "one\ntwo\nthree"


async def test_numbering_continues_across_pages(app, ctx, project):
    (project / "big.txt").write_text("\n".join(f"line {i}" for i in range(30)), encoding="utf-8")
    first = await run(app, ctx, "fs_read", {"path": str(project / "big.txt"), "offset": 0, "limit": 10})
    assert first.content.startswith("1\tline 0")
    assert first.content.splitlines()[-2] == "10\tline 9"
    assert "[lines 1–10 of 30; fs_read(path, offset=10) for more]" in first.content

    second_offset = first.meta["end"]
    assert second_offset == 10
    second = await run(app, ctx, "fs_read", {"path": str(project / "big.txt"), "offset": second_offset, "limit": 10})
    assert second.content.startswith("11\tline 10")
    assert "[lines 11–20 of 30; fs_read(path, offset=20) for more]" in second.content


async def test_char_cap_does_not_split_a_line(app, ctx, project):
    (project / "wide.txt").write_text("\n".join("x" * 200 for _ in range(200)), encoding="utf-8")
    result = await run(app, ctx, "fs_read", {"path": str(project / "wide.txt"), "limit": 400})
    assert result.ok
    assert len(result.content) < 9000
    for line in result.content.splitlines()[:-1]:  # last line is the footer
        if line.startswith("["):
            continue
        assert "\t" in line
    assert "for more]" in result.content
    # No returned line was truncated mid-line (each is a full "N\txxxx...x" of 200 x's,
    # plus the number prefix), except possibly the very last one kept.
    body = result.content.rsplit("\n[", 1)[0]
    lines = body.splitlines()
    for line in lines[:-1]:
        _, _, rest = line.partition("\t")
        assert rest == "x" * 200


async def test_past_the_end_reports_total_lines(app, ctx, project):
    (project / "a.txt").write_text("one", encoding="utf-8")
    result = await run(app, ctx, "fs_read", {"path": str(project / "a.txt"), "offset": 50})
    assert "past the end" in result.content


async def test_single_line_longer_than_cap_is_cut_but_the_read_advances(app, ctx, project, monkeypatch):
    monkeypatch.setattr(app.settings, "get", lambda key: 20 if key == "conversation.tool_output_cap" else 10)
    (project / "huge.txt").write_text("y" * 500 + "\nsecond line", encoding="utf-8")
    result = await run(app, ctx, "fs_read", {"path": str(project / "huge.txt")})
    assert result.ok
    assert len(result.content.split("\n[")[0]) <= 20
    assert result.meta["end"] == 1  # the read still advanced past the oversized line
