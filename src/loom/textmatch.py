"""The one text-matching rule behind every old→new edit: `fs_edit`, the notebook's
cells, and the vault's `replace` (03 §9.2, §4.3). Stdlib only, imported by both the
tools and the knowledge packages, so it lives beside neither.

Exact text first. When nothing matches exactly, the same lines compared with line
endings and trailing whitespace ignored — the two things a transcript cannot show, so
the model cannot copy them. Everything the model can see — indentation, wording, where
a line breaks — still has to match: forgiving those would absorb a weaker model's
mistakes (charter §2.1). A miss is told where it missed (2026-09-29).
"""

from __future__ import annotations

import difflib


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _canon_lines(text: str) -> list[str]:
    """The text's lines with the two things a transcript cannot show removed: the
    line-ending style and trailing whitespace."""
    return [line.rstrip() for line in _lf(text).split("\n")]


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def find_spans(text: str, old: str) -> list[tuple[int, int]]:
    """Every `(start, end)` of `text` that `old` matches, non-overlapping, in order.

    Exact matches first. When there are none, the same lines compared with line
    endings and trailing whitespace ignored (2026-09-29: a thread sent CRLF `old`
    against an LF file, and trailing spaces it could not see, and was told to "copy
    the text exactly" five times). Spans always index the original `text`, so the
    characters around a match stay exactly as they were. One exact occurrence wins
    over loose ones: exact is the more specific match."""
    if not old:
        return []
    spans: list[tuple[int, int]] = []
    at = text.find(old)
    while at != -1:
        spans.append((at, at + len(old)))
        at = text.find(old, at + len(old))
    if spans:
        return spans

    if _lf(text) != text:
        # a file with mixed endings is kept verbatim (`TextFile.newline == ""`); its
        # offsets differ from the LF view, so the loose match is not attempted
        return []
    old_lines = _canon_lines(old)
    lines = _canon_lines(text)
    starts = _line_starts(text)
    k = len(old_lines)
    if k == 1:
        needle = old_lines[0]
        if not needle:
            return []
        for i, line in enumerate(lines):
            at = line.find(needle)
            while at != -1:
                spans.append((starts[i] + at, starts[i] + at + len(needle)))
                at = line.find(needle, at + len(needle))
        return spans
    first, middle, last = old_lines[0], old_lines[1:-1], old_lines[-1]
    last_end = 0
    for i in range(0, len(lines) - k + 1):
        if starts[i] < last_end:
            continue
        if not lines[i].endswith(first) or lines[i + 1 : i + k - 1] != middle or not lines[i + k - 1].startswith(last):
            continue
        start = starts[i] + len(lines[i]) - len(first)
        end = starts[i + k - 1] + len(last)
        spans.append((start, end))
        last_end = end
    return spans


def replace_spans(text: str, spans: list[tuple[int, int]], new: str) -> str:
    out: list[str] = []
    at = 0
    for start, end in spans:
        out.append(text[at:start])
        out.append(new)
        at = end
    out.append(text[at:])
    return "".join(out)


def line_of(text: str, offset: int) -> int:
    """1-based line number of `offset` in `text`."""
    return text.count("\n", 0, offset) + 1


def lines_of(text: str, spans: list[tuple[int, int]], limit: int = 12) -> str:
    """"lines 3, 9" for the spans, capped, for an ambiguity message."""
    numbers = ", ".join(str(line_of(text, start)) for start, _ in spans[:limit])
    return f"lines {numbers}" + ("…" if len(spans) > limit else "")


CLOSEST_MAX_LINES = 20_000  # a text bigger than this gets no closest-match search
CLOSEST_DIFF_LINES = 30


def closest_excerpt(text: str, old: str) -> str:
    """Where `old` nearly is: the text's lines that best align with it, as a diff of the
    file (-) against `old` (+), so a miss can be corrected against what is there
    instead of guessed at again. A thread retried one wrong `old` six times on a
    message that only said "copy the text exactly" (2026-09-29). Empty when nothing in
    the text resembles it, or the text is too large to search."""
    old_lines = _canon_lines(old)
    while old_lines and not old_lines[-1]:
        old_lines.pop()
    lines = _canon_lines(text)
    if not old_lines or not lines or len(lines) > CLOSEST_MAX_LINES:
        return ""
    matcher = difflib.SequenceMatcher(None, lines, old_lines, autojunk=False)
    block = matcher.find_longest_match(0, len(lines), 0, len(old_lines))
    if block.size and any(lines[block.a : block.a + block.size]):
        top = block.a - block.b
    else:
        anchor = next((line for line in old_lines if line.strip()), "")
        near = difflib.get_close_matches(anchor, [line for line in lines if line.strip()], n=1, cutoff=0.6)
        if not near:
            return ""
        top = lines.index(near[0])
    top = max(0, min(top, len(lines) - 1))
    bottom = min(len(lines), top + len(old_lines))
    region = lines[top:bottom]
    diff = list(
        difflib.unified_diff(region, old_lines, fromfile="file", tofile="old", lineterm="", n=len(old_lines))
    )[2:]  # the ---/+++ header lines say nothing the label does not
    if len(diff) > CLOSEST_DIFF_LINES:
        diff = diff[:CLOSEST_DIFF_LINES] + [f"… ({len(diff) - CLOSEST_DIFF_LINES} more lines)"]
    where = f"line {top + 1}" if bottom - top <= 1 else f"lines {top + 1}–{bottom}"
    return f"The closest text is at {where}; the file (-) against your `old` (+):\n" + "\n".join(diff)
