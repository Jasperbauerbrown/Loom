"""fs_list and fs_search: finding files and finding text in them (03 §9.2).

Unchanged by the 2026-09-24 fs work beyond the move into this package; the ignore-aware
search of audit #4 is still on the backlog.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Iterator

from ...registry import ToolContext, ToolResult
from .common import is_binary, iter_paths, read_text, resolve, with_base_hint

SEARCH_MAX_BYTES = 2 * 1024 * 1024


# ---------------------------------------------------------------------------------
# fs_list
# ---------------------------------------------------------------------------------
def _list_sync(root: Path, pattern: str, max_results: int, include_hidden: bool) -> ToolResult:
    if not root.exists():
        return ToolResult.error(f"no such directory: {root}")
    if not root.is_dir():
        return ToolResult.error(f"{root} is a file; use fs_read")
    rows: list[str] = []
    truncated = False
    for item in iter_paths(root, pattern, include_hidden):
        try:
            rel = item.relative_to(root).as_posix()
            rows.append(rel + "/" if item.is_dir() else rel)
        except (OSError, ValueError):
            continue
        if len(rows) >= max_results:
            truncated = True
            break
    rows.sort()
    if not rows:
        return ToolResult(content=f"[no entries under {root} matching {pattern}]", meta={"count": 0})
    footer = f"\n[{len(rows)} entries{'; more were dropped, narrow the pattern or raise max_results' if truncated else ''}]"
    return ToolResult(content="\n".join(rows) + footer, meta={"count": len(rows), "truncated": truncated})


async def fs_list_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    root = resolve(ctx, str(args["path"]))
    pattern = str(args.get("pattern") or "**/*")
    max_results = int(args.get("max_results") or 500)
    include_hidden = bool(args.get("include_hidden") or False)
    return with_base_hint(ctx, str(args["path"]), await asyncio.to_thread(_list_sync, root, pattern, max_results, include_hidden))


# ---------------------------------------------------------------------------------
# fs_search
# ---------------------------------------------------------------------------------
def _search_files(root: Path, glob: str, include_hidden: bool = False) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for item in iter_paths(root, glob, include_hidden):
        try:
            if item.is_file():
                yield item
        except OSError:
            continue


def _search_sync(root: Path, pattern: str, glob: str, context: int, max_results: int, case_sensitive: bool) -> ToolResult:
    if not root.exists():
        return ToolResult.error(f"no such path: {root}")
    try:
        regex = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        return ToolResult.error(f"invalid regular expression {pattern!r}: {exc}")
    blocks: list[str] = []
    matches = 0
    files_with_matches = 0
    skipped = 0
    truncated = False
    for file in _search_files(root, glob):
        if matches >= max_results:
            truncated = True
            break
        try:
            if file.stat().st_size > SEARCH_MAX_BYTES or is_binary(file):
                skipped += 1
                continue
            lines = read_text(file).splitlines()
        except OSError:
            skipped += 1
            continue
        hit_here = False
        for i, line in enumerate(lines):
            if not regex.search(line):
                continue
            hit_here = True
            matches += 1
            start = max(0, i - context)
            end = min(len(lines), i + context + 1)
            rel = file.as_posix()
            body = [f"{rel}:{n + 1}:{'>' if n == i else ' '} {lines[n]}" for n in range(start, end)]
            blocks.append("\n".join(body))
            if matches >= max_results:
                truncated = True
                break
        if hit_here:
            files_with_matches += 1
    if not blocks:
        note = f" ({skipped} files skipped as binary or oversized)" if skipped else ""
        return ToolResult(content=f"[no matches for {pattern!r} under {root}{note}]", meta={"matches": 0})
    footer = f"\n[{matches} matches in {files_with_matches} files"
    footer += "; stopped at max_results" if truncated else ""
    footer += f"; {skipped} files skipped" if skipped else ""
    footer += "]"
    return ToolResult(
        content="\n--\n".join(blocks) + footer,
        meta={"matches": matches, "files": files_with_matches, "truncated": truncated},
    )


async def fs_search_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    root = resolve(ctx, str(args["path"]))
    return await asyncio.to_thread(
        _search_sync,
        root,
        str(args["pattern"]),
        str(args.get("glob") or "**/*"),
        max(0, int(args.get("context") if args.get("context") is not None else 2)),
        max(1, int(args.get("max_results") or 100)),
        bool(args.get("case_sensitive") or False),
    )
