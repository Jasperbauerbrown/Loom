"""`process(action="logs")` argument surface: tail, since, offset, and the footer that
always names the next call (finding F8, docs/reference/tower-defense-run-audit.md §2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from loom import db
from loom.tools import dispatch, fs_guard
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


def _make_process(app, project: Path, process_id: str, text: str) -> str:
    """A process row plus its log file, without spawning a child (mirrors the idiom in
    tests/test_processes.py's `_insert`)."""
    log_path = project / f"{process_id}.log"
    log_path.write_text(text, encoding="utf-8")
    app.db.execute(
        "INSERT INTO processes(id, conversation_id, title, command, cwd, status, log_path, started_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (process_id, app.desk_id(), process_id, "x", str(project), "exited", str(log_path), db.now_iso()),
    )
    return process_id


async def _logs(app, ctx, **args):
    tool = app.tools.get("process")
    args["action"] = "logs"
    return await dispatch.execute(app, ctx, tool, args)


async def test_tail_reads_the_end_of_a_long_log(app, ctx, project):
    text = "0123456789" * 100  # 1000 chars
    pid = _make_process(app, project, "p-tail", text)
    app.settings.set(app.db, "conversation.tool_output_cap", "500")
    res = await _logs(app, ctx, id=pid, tail=True)
    assert res.ok
    body = res.content.split("\n[chars")[0]
    assert body == text[-500:]
    assert f'since=1000) for what is written next]' in res.content
    assert res.meta == {"id": pid, "offset": 500, "end": 1000, "total": 1000}


async def test_since_walks_a_growing_log_with_no_gaps_or_repeats(app, ctx, project):
    text = "abcdefghij" * 150  # 1500 chars
    pid = _make_process(app, project, "p-since", text)
    app.settings.set(app.db, "conversation.tool_output_cap", "500")
    first = await _logs(app, ctx, id=pid, since=0)
    assert first.ok and first.content.startswith("abcdefghij")
    assert 'since=500) for what follows, tail=true for the end]' in first.content

    collected = first.content.split("\n[chars")[0]
    since = 500
    while since < 1500:
        page = await _logs(app, ctx, id=pid, since=since)
        assert page.ok
        body = page.content.split("\n[chars")[0]
        collected += body
        since = page.meta["end"]
    assert collected == text
    # the last page names the log's true end and says there's nothing more to chase
    assert f'since=1500) for what is written next]' in page.content
    assert 'for what follows' not in page.content


async def test_since_past_the_end_is_an_empty_page_not_an_error(app, ctx, project):
    text = "hello world"
    pid = _make_process(app, project, "p-past", text)
    res = await _logs(app, ctx, id=pid, since=10_000)
    assert res.ok
    assert res.content.startswith("\n[chars") or res.content.strip().startswith("[chars")
    assert f'chars 11–11 of 11; process(action="logs", id="{pid}", since=11) for what is written next]' in res.content
    assert res.meta == {"id": pid, "offset": 11, "end": 11, "total": 11}


async def test_offset_still_behaves_as_it_did_before(app, ctx, project):
    text = "x" * 20
    pid = _make_process(app, project, "p-offset", text)
    res = await _logs(app, ctx, id=pid, offset=5, limit=5)
    assert res.ok
    assert res.content.startswith("xxxxx\n[chars 5–10 of 20;")
    assert f'since=10) for what follows, tail=true for the end]' in res.content


async def test_tail_wins_over_since_and_offset(app, ctx, project):
    text = "y" * 600
    pid = _make_process(app, project, "p-precedence", text)
    app.settings.set(app.db, "conversation.tool_output_cap", "500")
    res = await _logs(app, ctx, id=pid, tail=True, since=0, offset=0)
    assert res.ok
    assert res.meta["offset"] == 100 and res.meta["end"] == 600


async def test_short_log_fits_in_one_page_and_still_gets_a_footer(app, ctx, project):
    text = "short log"
    pid = _make_process(app, project, "p-short", text)
    res = await _logs(app, ctx, id=pid)
    assert res.ok
    assert res.content.startswith("short log\n[chars 0–9 of 9;")
    assert f'since=9) for what is written next]' in res.content


async def test_empty_log_says_no_output_yet_and_still_carries_a_footer(app, ctx, project):
    pid = _make_process(app, project, "p-empty", "")
    res = await _logs(app, ctx, id=pid)
    assert res.ok
    assert res.content.startswith("(no output yet)\n[chars 0–0 of 0;")
    assert f'since=0) for what is written next]' in res.content
    assert res.meta == {"id": pid, "offset": 0, "end": 0, "total": 0}


async def test_bad_since_is_an_error_result_not_an_exception(app, ctx, project):
    pid = _make_process(app, project, "p-bad", "abc")
    neg = await _logs(app, ctx, id=pid, since=-1)
    assert not neg.ok and "since" in neg.content

    non_int = await _logs(app, ctx, id=pid, since="not-a-number")
    assert not non_int.ok and "since" in non_int.content

    bad_offset = await _logs(app, ctx, id=pid, offset=-5)
    assert not bad_offset.ok and "since" in bad_offset.content


async def test_list_and_stop_are_untouched(app, ctx, project):
    pid = _make_process(app, project, "p-list", "abc")
    tool = app.tools.get("process")
    listed = await dispatch.execute(app, ctx, tool, {"action": "list"})
    assert listed.ok and pid in listed.content

    stopped = await dispatch.execute(app, ctx, tool, {"action": "stop", "id": pid})
    assert stopped.ok and "is not running: exited" in stopped.content
