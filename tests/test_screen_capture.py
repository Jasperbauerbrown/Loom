"""The `screenshot` tool's seams (03 §9.2, docs/reference/tower-defense-run-audit.md
F2): the generated Windows script, CLIXML decoding, and the error path — none of
which can run headless in CI, so these tests do not touch a real screen."""

from __future__ import annotations

import pytest

from loom.tools.native import screen as screen_tool
from loom.tools.native.screen import CaptureError, CaptureSpec, decode_clixml
from loom.tools.registry import ToolContext


@pytest.fixture
def ctx(app):
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1", event_id=1)


async def run(app, ctx, args):
    return await app.tools.get("screenshot").handler(ctx, args)


# ---------------------------------------------------------------------------------
# _windows_script
# ---------------------------------------------------------------------------------
def test_window_spec_takes_the_printwindow_path_with_a_copyfromscreen_fallback():
    spec = CaptureSpec(out_path="x.png", window="Tower Defense")
    script = screen_tool._windows_script(spec)
    assert "PrintWindow" in script
    assert "0x2" in script
    assert "CopyFromScreen" in script
    # PrintWindow is only reached inside the $win branch.
    idx_branch = script.index("if ($win -ne '')")
    idx_printwindow_call = script.index("PrintWindow($hwnd")
    assert idx_printwindow_call > idx_branch
    # The fallback (CopyFromScreen) still runs when PrintWindow fails or is all black.
    assert "capturedByWindow" in script


def test_region_spec_does_not_populate_win_so_printwindow_is_never_reached():
    spec = CaptureSpec(out_path="x.png", region={"x": 0, "y": 0, "width": 10, "height": 10})
    script = screen_tool._windows_script(spec)
    assert "$win = ''" in script
    assert "CopyFromScreen" in script


def test_monitor_spec_does_not_populate_win_so_printwindow_is_never_reached():
    spec = CaptureSpec(out_path="x.png", monitor=2)
    script = screen_tool._windows_script(spec)
    assert "$win = ''" in script


def test_window_title_with_a_quote_is_escaped_for_the_ps_single_quoted_literal():
    spec = CaptureSpec(out_path="x.png", window="it's a game")
    script = screen_tool._windows_script(spec)
    assert "$win = 'it''s a game'" in script


# ---------------------------------------------------------------------------------
# decode_clixml
# ---------------------------------------------------------------------------------
def test_decode_clixml_extracts_the_real_sentence():
    blob = (
        "#< CLIXML\r\n"
        '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
        '<S S="Error">no window whose title contains "tower"_x000D__x000A_</S>'
        '<S S="Error">At line:1 char:1_x000D__x000A_</S>'
        "</Objs>"
    )
    decoded = decode_clixml(blob)
    assert 'no window whose title contains "tower"' in decoded
    assert "CLIXML" not in decoded


def test_decode_clixml_passes_plain_text_through_unchanged():
    assert decode_clixml('no window whose title contains "x"') == 'no window whose title contains "x"'


def test_decode_clixml_never_raises_on_malformed_input():
    garbage = "#< CLIXML\nnot actually xml <S S=\"Error\" unterminated"
    decoded = decode_clixml(garbage)
    assert isinstance(decoded, str)
    assert "CLIXML" not in decoded


def test_decode_clixml_empty_body_falls_back_to_original():
    decoded = decode_clixml("#< CLIXML")
    assert isinstance(decoded, str)


# ---------------------------------------------------------------------------------
# error path: the model must see the real sentence, never CLIXML
# ---------------------------------------------------------------------------------
async def test_capture_surfaces_the_real_message_from_a_clixml_failure(monkeypatch):
    blob = (
        "#< CLIXML\r\n"
        '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
        '<S S="Error">no window whose title contains "tower"_x000D__x000A_</S>'
        "</Objs>"
    )

    async def fake_run(argv):
        return 1, screen_tool.decode_clixml(blob)

    monkeypatch.setattr(screen_tool, "_run", fake_run)
    monkeypatch.setattr(screen_tool.os, "name", "nt")
    spec = CaptureSpec(out_path="unused.png", window="tower")
    with pytest.raises(CaptureError) as excinfo:
        await screen_tool.capture(spec)
    message = str(excinfo.value)
    assert 'no window whose title contains "tower"' in message
    assert "CLIXML" not in message


async def test_capture_surfaces_a_plain_text_failure_unchanged(monkeypatch):
    async def fake_run(argv):
        return 1, 'no window whose title contains "x"'

    monkeypatch.setattr(screen_tool, "_run", fake_run)
    monkeypatch.setattr(screen_tool.os, "name", "nt")
    spec = CaptureSpec(out_path="unused.png", window="x")
    with pytest.raises(CaptureError) as excinfo:
        await screen_tool.capture(spec)
    assert 'no window whose title contains "x"' in str(excinfo.value)


# ---------------------------------------------------------------------------------
# handler argument validation (not covered in tests/test_tools_native.py)
# ---------------------------------------------------------------------------------
async def test_screenshot_rejects_bad_scale(app, ctx):
    result = await run(app, ctx, {"scale": 5})
    assert not result.ok
    assert "scale" in result.content


async def test_screenshot_rejects_bad_region(app, ctx):
    result = await run(app, ctx, {"region": {"x": 0, "y": 0, "width": 0, "height": 10}})
    assert not result.ok
    assert "region" in result.content


async def test_screenshot_rejects_bad_format(app, ctx):
    result = await run(app, ctx, {"format": "bmp"})
    assert not result.ok
    assert "format" in result.content


async def test_screenshot_rejects_negative_monitor(app, ctx):
    result = await run(app, ctx, {"monitor": -1})
    assert not result.ok
    assert "monitor" in result.content
