"""fs_read: one page of a file, as text a model can cite.

Text, PDF (through `pdftotext`) and notebooks (rendered cells) all arrive here as lines
and are paged the same way; images come back whole, as the image.
"""

from __future__ import annotations

import asyncio
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


def _read_sync(
    path: Path,
    offset: int,
    limit: int,
    line_numbers: bool = True,
    layout: bool = True,
    image_cap: int = 10 * 1024 * 1024,
    char_cap: int = 8000,
) -> ToolResult:
    """`char_cap` is the page: `conversation.tool_output_cap`, the same number the
    dispatcher spills at. fs_read is a paged tool, so the dispatcher leaves its result
    alone and the footer rides on top of the page."""
    if path.is_dir():
        return ToolResult.error(f"{path} is a directory; use fs_list")
    if not path.exists():
        return ToolResult.error(f"no such file: {path}")
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
        return ToolResult.error(f"{path} is a binary file; fs_read returns text and images (png, jpeg, webp, gif) only")
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
    offset = int(args.get("offset") or 0)
    limit = int(args.get("limit") or 400)
    line_numbers = bool(args.get("line_numbers", True))
    layout = bool(args.get("layout", True))
    image_cap = int(ctx.app.settings.get("conversation.image_max_mb")) * 1024 * 1024
    char_cap = int(ctx.app.settings.get("conversation.tool_output_cap"))
    result = await asyncio.to_thread(_read_sync, path, offset, limit, line_numbers, layout, image_cap, char_cap)
    if result.ok:
        # The ledger: what this conversation has now seen of the file, so a later write
        # can tell whether it changed underneath (audit #2).
        ledger.record(ctx.app, ctx.conversation_id, path)
    return with_base_hint(ctx, raw, result)
