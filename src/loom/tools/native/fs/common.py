"""Shared plumbing for the fs tools: path resolution, the "you meant the vault" hint,
binary sniffing, and the text round-trip that keeps a file's line endings and BOM.

Every fs handler resolves its path the same way (`resolve`), reports a missed relative
path the same way (`with_base_hint`), and — on the writing side — loads and stores text
through `TextFile`, so a file written by CRLF Windows stays CRLF and a BOM survives an
edit (audit #18).
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ...fs_guard import normalise
from ...registry import ToolContext, ToolResult

BINARY_SNIFF = 8192
STYLE_SNIFF = 64 * 1024
BOM = "﻿"
BOM_BYTES = BOM.encode("utf-8")


# ---------------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------------
def base(ctx: ToolContext) -> str | None:
    roots = ctx.roots()
    return roots[0] if roots else None


def resolve(ctx: ToolContext, path: str) -> Path:
    return Path(normalise(path, cwd=base(ctx)))


def _is_relative(raw: str) -> str | None:
    """The cleaned spelling when `raw` is a relative path, else None."""
    cleaned = str(raw).strip().strip('"').strip("'").replace("\\", "/")
    if not cleaned or cleaned.startswith(("/", "~")) or (len(cleaned) > 1 and cleaned[1] == ":"):
        return None
    return cleaned


def vault_note(ctx: ToolContext, raw: str) -> str | None:
    """The vault-relative note this relative path names, when it names one. A thread
    spent a run writing `projects/x.md` into the artifacts root while meaning the vault
    note of that name (TD audit F9); the fs tools say so instead of creating it."""
    cleaned = _is_relative(raw)
    if not cleaned:
        return None
    vault = getattr(ctx.app, "vault", None)
    if vault is None:
        return None
    for candidate in (cleaned, cleaned + ".md"):
        try:
            if vault.exists(candidate):
                return vault.norm(candidate)
        except Exception:
            return None
    return None


def with_base_hint(ctx: ToolContext, raw: str, result: ToolResult) -> ToolResult:
    """A relative path that missed: say what it was resolved against, so the model can
    correct the path instead of hunting for the file (a thread spent twenty calls on
    `projects/x.md` landing in the artifacts root, 2026-09-21) — and, when the path
    names an existing vault note, say which tool owns it (TD audit F9)."""
    if result.ok or "no such" not in result.content:
        return result
    if not _is_relative(raw):
        return result
    note = vault_note(ctx, raw)
    if note:
        return ToolResult(
            content=f"{result.content} ({note} is a vault note: read it with knowledge_read and change it with knowledge_write)",
            ok=False,
            meta=result.meta,
        )
    root = base(ctx)
    if not root:
        return result
    return ToolResult(
        content=f"{result.content} (relative paths resolve against the first allowed root, {root})",
        ok=False,
        meta=result.meta,
    )


def vault_refusal(ctx: ToolContext, raw: str, path: Path) -> ToolResult | None:
    """The writing tools' half of F9: a relative path that does not exist under the
    roots but names an existing vault note is a mistake, not a new file."""
    if path.exists():
        return None
    note = vault_note(ctx, raw)
    if not note:
        return None
    return ToolResult.error(
        f"no such file: {path.as_posix()}; {note} is a vault note — read it with knowledge_read "
        "and change it with knowledge_write (the fs tools do not write the vault)"
    )


# ---------------------------------------------------------------------------------
# reading bytes
# ---------------------------------------------------------------------------------
def is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(BINARY_SNIFF)
    except OSError:
        return False


def read_text(path: Path) -> str:
    """Lossy decode, for reading and searching only. Writers use `load_text`."""
    return path.read_text(encoding="utf-8", errors="replace")


def iter_paths(root: Path, pattern: str, include_hidden: bool) -> Iterator[Path]:
    """`root.glob(pattern)` with hidden entries filtered and unreadable branches skipped."""
    try:
        candidates = root.glob(pattern)
    except (ValueError, OSError):
        return
    while True:
        try:
            item = next(candidates)  # type: ignore[arg-type]
        except StopIteration:
            return
        except OSError:
            continue
        if not include_hidden and any(part.startswith(".") for part in item.relative_to(root).parts):
            continue
        yield item


# ---------------------------------------------------------------------------------
# text round-trip: line endings and BOM (audit #18)
# ---------------------------------------------------------------------------------
class TextError(Exception):
    """A file the writing tools cannot round-trip (not UTF-8)."""


@dataclass
class TextFile:
    """A file's text with the two things a rewrite must not silently change.

    `text` is always LF-terminated with no BOM, which is what `old`/`new` are matched
    against. `newline` is the ending to write back: "\\n", "\\r\\n", or "" for a file
    whose endings are mixed — those are left exactly as they came, because picking a
    winner would rewrite lines nobody touched.
    """

    text: str
    newline: str = "\n"
    bom: bool = False

    @property
    def crlf(self) -> bool:
        return self.newline == "\r\n"

    def encode(self, text: str) -> bytes:
        """`text` (LF, no BOM) as this file's bytes."""
        if self.newline == "\r\n":
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        if self.bom:
            text = BOM + text
        return text.encode("utf-8")


def sniff_style(data: bytes) -> tuple[str, bool]:
    """`(newline, bom)` for these bytes, without decoding them. A file whose endings are
    mixed gets `""`: nothing is normalised, so no untouched line is rewritten."""
    bom = data.startswith(BOM_BYTES)
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n")
    if crlf == 0:
        newline = "\n"
    elif crlf == lf:
        newline = "\r\n"
    else:
        newline = ""
    return newline, bom


def style_of(path: Path) -> tuple[str, bool]:
    """The line ending and BOM of an existing file; LF and no BOM when it is new."""
    try:
        with path.open("rb") as fh:
            return sniff_style(fh.read(STYLE_SNIFF))
    except OSError:
        return "\n", False


def decode_text(data: bytes, *, path: Path | None = None) -> TextFile:
    """Bytes → `TextFile`. Raises `TextError` for anything that is not UTF-8: a lossy
    decode would write the replacement characters back into the user's file."""
    newline, bom = sniff_style(data)
    if bom:
        data = data[len(BOM_BYTES) :]
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        where = path.as_posix() if path else "the file"
        raise TextError(f"{where} is not valid UTF-8 ({exc.reason}); the fs tools edit UTF-8 text") from exc
    text = raw.replace("\r\n", "\n") if newline == "\r\n" else raw
    return TextFile(text=text, newline=newline, bom=bom)


def load_text(path: Path) -> TextFile:
    return decode_text(path.read_bytes(), path=path)


def write_atomic(path: Path, data: bytes) -> None:
    """Write beside the target and replace it, so a failed or interrupted write cannot
    leave a half-written file where a whole one was."""
    tmp = path.with_name(path.name + ".loom-tmp")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------------
# one edit, one matching rule
# ---------------------------------------------------------------------------------
@dataclass
class Edit:
    """One old→new replacement. `fs_edit` takes a list of these; a patch hunk becomes
    one; a notebook cell edit is matched against cell sources with the same rule."""

    old: str
    new: str
    replace_all: bool = False


class EditError(Exception):
    """An edit that does not apply. The message is what the model is told."""


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
    endings and trailing whitespace ignored — the two things the transcript cannot
    show, so the model cannot copy them (2026-09-29: a thread sent CRLF `old` against
    an LF file, and trailing spaces it could not see, and was told to "copy the text
    exactly" five times). Spans always index the original `text`, so the characters
    around a match stay exactly as they were. Anything else — indentation, wording, a
    line break moved — still has to match: those the model can see."""
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


CLOSEST_MAX_LINES = 20_000  # a file bigger than this gets no closest-match search
CLOSEST_DIFF_LINES = 30


def closest_excerpt(text: str, old: str) -> str:
    """Where `old` nearly is: the file's lines that best align with it, as a diff of the
    file (-) against `old` (+), so a miss can be corrected against what is there
    instead of guessed at again. A thread retried one wrong `old` six times on a
    message that only said "copy the text exactly" (2026-09-29). Empty when nothing in
    the file resembles it, or the file is too large to search."""
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


def edit_problem(count: int, replace_all: bool, where: str, detail: str = "") -> str | None:
    """The one matching rule, stated once: the text, unique unless `replace_all`.
    `detail` is what the caller can add about the miss — the closest match, the lines
    the duplicates are on."""
    if count == 0:
        return (
            f"`old` text not found in {where}; read the file and copy the text exactly "
            "(fs_read's `N\\t` line-number prefixes are not part of the text)"
            + (f"\n{detail}" if detail else "")
        )
    if count > 1 and not replace_all:
        return (
            f"`old` text appears {count} times in {where}{detail}; include more surrounding "
            "context to make it unique, or pass replace_all=true"
        )
    return None


def apply_edit(text: str, edit: Edit, where: str) -> tuple[str, int]:
    """`(new_text, replacements)`, or `EditError` with the reason it did not apply.
    `new` is written with LF line breaks whatever it arrived with; the file's own
    ending is put back by `TextFile.encode`."""
    if not edit.old:
        raise EditError(f"`old` is empty in {where}; it must be the exact text to replace")
    spans = find_spans(text, edit.old)
    if not spans:
        detail = closest_excerpt(text, edit.old)
    elif len(spans) > 1:
        detail = " (at lines " + ", ".join(str(line_of(text, s)) for s, _ in spans[:12]) + ("…" if len(spans) > 12 else "") + ")"
    else:
        detail = ""
    problem = edit_problem(len(spans), edit.replace_all, where, detail)
    if problem:
        raise EditError(problem)
    new = edit.new.replace("\r\n", "\n")
    if edit.replace_all:
        return replace_spans(text, spans, new), len(spans)
    return replace_spans(text, spans[:1], new), 1


def unified_excerpt(before: str, after: str, path: str, width: int = 60, context: int = 2) -> str:
    """The diff a writing tool shows for what it did, bounded so one large edit cannot
    fill a page with its own diff."""
    lines = list(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), fromfile=path, tofile=path, lineterm="", n=context
        )
    )
    if len(lines) > width:
        lines = lines[:width] + [f"… ({len(lines) - width} more diff lines)"]
    return "\n".join(lines)
