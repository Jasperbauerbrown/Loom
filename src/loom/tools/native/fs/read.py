"""fs_read: one page of a file, as text a model can cite.

Text, PDF (through `pdftotext`) and notebooks (rendered cells) all arrive here as lines
and are paged the same way; images come back whole, as the image.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from ....artifacts import image_dimensions, sniff_image
from ...registry import ToolContext, ToolResult
from . import ledger, notebook, pdf
from .common import BOM, is_binary, read_text, resolve, with_base_hint


# ---------------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------------
def _read_image(path: Path, mime: str, cap: int) -> ToolResult:
    """An image file comes back whole, as the image (offset/limit do not apply)."""
    size = path.stat().st_size
    if size > cap:
        return ToolResult.error(
            f"{path} is a {size}-byte {mime}, over the {cap // (1024 * 1024)} MB image cap (conversation.image_max_mb); downscale it first"
        )
    data = path.read_bytes()
    dims = image_dimensions(data)
    shape = f", {dims[0]}×{dims[1]}" if dims else ""
    return ToolResult(
        content=f"{path}: {mime}{shape}, {size} bytes",
        images=[{"data": data, "mime": mime}],
        meta={"path": str(path), "mime": mime, "bytes": size},
    )


# ---------------------------------------------------------------------------------
# binary modes: hexdump and printable-string extraction (stdlib only)
# ---------------------------------------------------------------------------------
HEX_BYTES_PER_LINE = 16
STRINGS_MIN_LEN = 4


def _hex_footer(path: Path, offset: int, end: int, total: int) -> str:
    if end < total:
        return f'\n[bytes {offset}–{end} of {total}; fs_read(path, offset={end}, binary="hex") for more]'
    return f"\n[bytes {offset}–{end} of {total}; end of file]"


def _hexdump(path: Path, offset: int, limit: int | None, char_cap: int) -> ToolResult:
    """A classic hexdump: an 8-hex-digit byte offset, 16 bytes in two groups of eight,
    then an ASCII gutter (printable 0x20–0x7E, else '.'). `offset`/`limit` are a BYTE
    offset and a BYTE count; the page is sized so its rendered text fits `char_cap`."""
    total = path.stat().st_size
    offset = max(0, int(offset))
    per_line = 8 + 2 + HEX_BYTES_PER_LINE * 3 + 1 + 2 + HEX_BYTES_PER_LINE + 1  # chars + newline
    cap_bytes = max(HEX_BYTES_PER_LINE, (char_cap // per_line) * HEX_BYTES_PER_LINE)
    window = cap_bytes if limit is None else min(max(1, int(limit)), cap_bytes)
    with path.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read(window)
    lines = []
    for i in range(0, len(chunk), HEX_BYTES_PER_LINE):
        row = chunk[i : i + HEX_BYTES_PER_LINE]
        addr = offset + i
        left = " ".join(f"{b:02x}" for b in row[:8])
        right = " ".join(f"{b:02x}" for b in row[8:])
        hexcol = (left + "  " + right).ljust(HEX_BYTES_PER_LINE * 3)
        gutter = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in row)
        lines.append(f"{addr:08x}  {hexcol} |{gutter}|")
    end = offset + len(chunk)
    body = "\n".join(lines) if lines else f"(no bytes at offset {offset}; file is {total} bytes)"
    return ToolResult(
        content=body + _hex_footer(path, offset, end, total),
        meta={"path": str(path), "binary": "hex", "bytes": total, "offset": offset, "end": end},
    )


def _strings(path: Path, offset: int, limit: int | None, char_cap: int, minlen: int = STRINGS_MIN_LEN) -> ToolResult:
    """Printable text runs — ASCII and UTF-16LE (common in PE binaries) — of at least
    `minlen` characters, emitted as `0xADDR: text`. `offset`/`limit` page over the file's
    BYTES; the footer names the next byte offset. The page is kept within `char_cap`."""
    total = path.stat().st_size
    offset = max(0, int(offset))
    scan = max(char_cap * 8, 4096) if limit is None else max(1, int(limit))
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read(scan)
    runs: list[tuple[int, str, int]] = []  # (abs offset, text, byte length)
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % minlen, data):
        runs.append((offset + m.start(), m.group().decode("ascii"), len(m.group())))
    for m in re.finditer(rb"(?:[\x20-\x7e]\x00){%d,}" % minlen, data):
        runs.append((offset + m.start(), m.group()[::2].decode("ascii"), len(m.group())))
    runs.sort(key=lambda r: r[0])
    scanned_end = offset + len(data)
    lines: list[str] = []
    running = 0
    next_off = scanned_end
    for addr, text, blen in runs:
        line = f"0x{addr:08x}: {text}"
        add = len(line) + (1 if lines else 0)
        if running + add > char_cap:
            if not lines:  # a single run longer than the whole cap: cut it so the read advances
                head = f"0x{addr:08x}: "
                lines.append(head + text[: max(1, char_cap - len(head))])
                next_off = addr + blen
            else:
                next_off = addr
            break
        running += add
        lines.append(line)
    if lines:
        body = "\n".join(lines)
    elif scanned_end < total:
        body = f"(no printable runs in bytes {offset}–{scanned_end})"
    else:
        body = "(no printable runs)"
    if next_off < total:
        footer = f'\n[bytes {offset}–{next_off} of {total}; fs_read(path, offset={next_off}, binary="strings") for more]'
    else:
        footer = f"\n[bytes {offset}–{next_off} of {total}; end of file]"
    return ToolResult(
        content=body + footer,
        meta={"path": str(path), "binary": "strings", "bytes": total, "offset": offset, "end": next_off},
    )


def _read_sync(
    path: Path,
    offset: int,
    limit: int,
    line_numbers: bool = True,
    layout: bool = True,
    image_cap: int = 10 * 1024 * 1024,
    char_cap: int = 8000,
    binary: str | None = None,
) -> ToolResult:
    """`char_cap` is the page: `conversation.tool_output_cap`, the same number the
    dispatcher spills at. fs_read is a paged tool, so the dispatcher leaves its result
    alone and the footer rides on top of the page."""
    if path.is_dir():
        return ToolResult.error(f"{path} is a directory; use fs_list")
    if not path.exists():
        return ToolResult.error(f"no such file: {path}")
    if binary in ("hex", "strings"):
        # Hex (and strings) of anything is valid, so this runs before the image/PDF/
        # notebook rendering and is not gated behind is_binary.
        try:
            return _hexdump(path, offset, limit, char_cap) if binary == "hex" else _strings(path, offset, limit, char_cap)
        except OSError as exc:
            return ToolResult.error(f"cannot read {path}: {exc}")
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError as exc:
        return ToolResult.error(f"cannot read {path}: {exc}")
    mime = sniff_image(head)
    if mime:
        try:
            return _read_image(path, mime, image_cap)
        except OSError as exc:
            return ToolResult.error(f"cannot read {path}: {exc}")
    if pdf.is_pdf(path, head):
        try:
            text = pdf.extract(path, layout=layout)
        except pdf.PdfError as exc:
            return ToolResult.error(str(exc))
        return page(
            text, path=path, offset=offset, limit=limit, line_numbers=line_numbers, char_cap=char_cap,
            meta={"kind": "pdf"},
        )
    if notebook.is_notebook(path):
        try:
            raw = read_text(path)
            text = notebook.render(raw)
            cells = notebook.cell_count(raw)
        except notebook.NotebookError as exc:
            return ToolResult.error(str(exc))
        except OSError as exc:
            return ToolResult.error(f"cannot read {path}: {exc}")
        return page(
            text, path=path, offset=offset, limit=limit, line_numbers=line_numbers, char_cap=char_cap,
            meta={"kind": "notebook", "cells": cells},
        )
    if is_binary(path):
        return ToolResult.error(
            f'{path} is a binary file; fs_read returns text and images (png, jpeg, webp, gif) only; '
            'pass binary="hex" or binary="strings" to read it'
        )
    try:
        content = read_text(path)
    except OSError as exc:
        return ToolResult.error(f"cannot read {path}: {exc}")
    if content.startswith(BOM):
        # A BOM is the file's encoding mark, not its first character. Shown, it ends up
        # copied into an `old` that then matches nothing (the edit side strips it too,
        # and puts it back on the write).
        content = content[len(BOM) :]
    return page(content, path=path, offset=offset, limit=limit, line_numbers=line_numbers, char_cap=char_cap)


def _fit_cap(rendered: list[str], char_cap: int) -> tuple[str, int, bool]:
    """`(text, kept_lines, capped)`: the one fitting rule — a page never ends mid-line,
    except a single line longer than the whole cap, which is cut so the read can still
    advance. `rendered` is already numbered (or not); this only fits it to the cap."""
    text = "\n".join(rendered)
    if len(text) <= char_cap:
        return text, len(rendered), False
    running = 0
    keep = 0
    for i, line in enumerate(rendered):
        added = len(line) + (1 if i else 0)  # the joining "\n" before every line but the first
        if running + added > char_cap:
            break
        running += added
        keep = i + 1
    if keep == 0:
        return rendered[0][:char_cap], 1, True
    return "\n".join(rendered[:keep]), keep, True


def page(
    content: str,
    *,
    path: Path,
    offset: int,
    limit: int,
    line_numbers: bool,
    char_cap: int,
    meta: dict[str, Any] | None = None,
) -> ToolResult:
    """Text → one page with a footer. The only paging codepath: text, PDF text and
    rendered notebooks all come through here.

    Each line is rendered `f"{n}\\t{line}"` with `n` the absolute 1-based line number
    (`offset + i + 1`) when `line_numbers` is set, so a later page continues the
    numbering; `line_numbers=False` returns the raw lines. The char cap applies to the
    rendered text, prefixes included.
    """
    lines = content.splitlines()
    total = len(lines)
    offset = max(0, offset)
    chunk = lines[offset : offset + max(1, limit)]
    if not chunk:
        return ToolResult(
            content=f"[{path} has {total} lines; offset {offset} is past the end]",
            meta={"path": str(path), "lines": total, **(meta or {})},
        )
    rendered = [f"{offset + i + 1}\t{line}" for i, line in enumerate(chunk)] if line_numbers else chunk
    text, kept, capped = _fit_cap(rendered, char_cap)
    end = offset + kept
    base_meta = {"path": str(path), "lines": total, "offset": offset, "end": end}
    base_meta.update(meta or {})
    footer = ""
    if end < total or capped:
        footer = f"\n[lines {offset + 1}–{end} of {total}; fs_read(path, offset={end}) for more]"
    return ToolResult(content=text + footer, meta=base_meta)


async def fs_read_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    raw = str(args["path"])
    path = resolve(ctx, raw)
    binary = args.get("binary")
    binary = str(binary) if binary in ("hex", "strings") else None
    offset = int(args.get("offset") or 0)
    # In binary mode offset/limit are bytes and a None limit lets the mode size its own
    # page; in text mode limit is lines with the usual default.
    raw_limit = args.get("limit")
    if binary:
        limit = None if raw_limit is None else int(raw_limit)
    else:
        limit = int(raw_limit or 400)
    line_numbers = bool(args.get("line_numbers", True))
    layout = bool(args.get("layout", True))
    image_cap = int(ctx.app.settings.get("conversation.image_max_mb")) * 1024 * 1024
    char_cap = int(ctx.app.settings.get("conversation.tool_output_cap"))
    result = await asyncio.to_thread(_read_sync, path, offset, limit, line_numbers, layout, image_cap, char_cap, binary)
    if result.ok:
        # The ledger: what this conversation has now seen of the file, so a later write
        # can tell whether it changed underneath (audit #2).
        ledger.record(ctx.app, ctx.conversation_id, path)
    return with_base_hint(ctx, raw, result)
