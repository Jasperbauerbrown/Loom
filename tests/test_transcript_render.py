"""`conversation_read` renders a result's harness verdict when the call was not simply
allowed (2026-09-28): a self-review reads permission friction off these lines."""

from loom.conversation import compaction


def _result(content: str, **meta):
    return {"kind": "tool_result", "content": content, "meta": {"ok": True, **meta}}


def test_a_plain_allow_says_nothing():
    text = compaction.render_for_summary([_result("fine", policy="allow", decision="allow", reason="")])
    assert text == "TOOL RESULT (ok): fine"


def test_an_asked_call_shows_who_decided_and_why():
    text = compaction.render_for_summary(
        [_result("listing", policy="ask", decision="allow", reason="C:/x is outside allowed roots")]
    )
    assert text == "TOOL RESULT (ok; ask → allow: C:/x is outside allowed roots): listing"


def test_classifier_and_auto_verdicts_render_and_errors_keep_their_status():
    evs = [
        _result("ran", policy="classify", decision="allow", reason="read-only command (static pre-filter)"),
        _result("ran", policy="auto", decision="allow", reason="desk auto-approve"),
        _result("error: denied by the person", ok=False, policy="ask", decision="deny", reason="x"),
    ]
    lines = compaction.render_for_summary(evs).splitlines()
    assert lines[0].startswith("TOOL RESULT (ok; classify → allow: read-only command")
    assert lines[1].startswith("TOOL RESULT (ok; auto → allow: desk auto-approve)")
    assert lines[2].startswith("TOOL RESULT (ERROR; ask → deny: x)")


def test_a_long_reason_is_cut_and_newlines_flattened():
    reason = "line one\nline two " + "x" * 300
    text = compaction.render_for_summary([_result("r", policy="classify", decision="ask", reason=reason)])
    assert "\n" not in text
    assert len(text) < 120 + 60
