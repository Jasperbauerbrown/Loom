"""Filesystem tools: fs_read, fs_list, fs_search, fs_write, fs_edit (03 §9.2).

All five take paths the guard has already resolved against the allowed roots; the
handlers do the work and report it in a shape a model can act on without guessing —
a footer that names the next offset rather than a bare truncation, a match count rather
than "not found", a diff rather than "ok".

Blocking file work runs in a worker thread so one large search cannot stall the event
loop and every other conversation with it.
"""

from __future__ import annotations

import asyncio
import difflib
import re
from pathlib import Path
from typing import Any, Iterator

from ... import prompts
from ..fs_guard import normalise
from ..registry import Tool, ToolContext, ToolResult

READ_CHAR_CAP = 8000
SEARCH_MAX_BYTES = 2 * 1024 * 1024
BINARY_SNIFF = 8192


# ---------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------
def _base(ctx: ToolContext) -> str | None:
    roots = ctx.roots()
    return roots[0] if roots else None


def _resolve(ctx: ToolContext, path: str) -> Path:
    return Path(normalise(path, cwd=_base(ctx)))


def is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(BINARY_SNIFF)
    except OSError:
        return False


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _iter_paths(root: Path, pattern: str, include_hidden: bool) -> Iterator[Path]:
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
# fs_read
# ---------------------------------------------------------------------------------
def _read_sync(path: Path, offset: int, limit: int) -> ToolResult:
    if path.is_dir():
        return ToolResult.error(f"{path} is a directory; use fs_list")
    if not path.exists():
        return ToolResult.error(f"no such file: {path}")
    if is_binary(path):
        return ToolResult.error(f"{path} is a binary file; fs_read returns text only")
    try:
        content = read_text(path)
    except OSError as exc:
        return ToolResult.error(f"cannot read {path}: {exc}")
    lines = content.splitlines()
    total = len(lines)
    offset = max(0, offset)
    chunk = lines[offset : offset + max(1, limit)]
    text = "\n".join(chunk)
    capped = False
    if len(text) > READ_CHAR_CAP:
        text = text[:READ_CHAR_CAP]
        chunk = text.splitlines()
        capped = True
    end = offset + len(chunk)
    if not chunk:
        return ToolResult(
            content=f"[{path} has {total} lines; offset {offset} is past the end]",
            meta={"path": str(path), "lines": total},
        )
    footer = ""
    if end < total or capped:
        footer = f"\n[lines {offset + 1}–{end} of {total}; fs_read(path, offset={end}) for more]"
    return ToolResult(content=text + footer, meta={"path": str(path), "lines": total, "offset": offset, "end": end})


async def fs_read_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve(ctx, str(args["path"]))
    offset = int(args.get("offset") or 0)
    limit = int(args.get("limit") or 400)
    return await asyncio.to_thread(_read_sync, path, offset, limit)


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
    for item in _iter_paths(root, pattern, include_hidden):
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
    root = _resolve(ctx, str(args["path"]))
    pattern = str(args.get("pattern") or "**/*")
    max_results = int(args.get("max_results") or 500)
    include_hidden = bool(args.get("include_hidden") or False)
    return await asyncio.to_thread(_list_sync, root, pattern, max_results, include_hidden)


# ---------------------------------------------------------------------------------
# fs_search
# ---------------------------------------------------------------------------------
def _search_files(root: Path, glob: str, include_hidden: bool = False) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for item in _iter_paths(root, glob, include_hidden):
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
            body = [
                f"{rel}:{n + 1}:{'>' if n == i else ' '} {lines[n]}" for n in range(start, end)
            ]
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
    root = _resolve(ctx, str(args["path"]))
    return await asyncio.to_thread(
        _search_sync,
        root,
        str(args["pattern"]),
        str(args.get("glob") or "**/*"),
        max(0, int(args.get("context") if args.get("context") is not None else 2)),
        max(1, int(args.get("max_results") or 100)),
        bool(args.get("case_sensitive") or False),
    )


# ---------------------------------------------------------------------------------
# fs_write
# ---------------------------------------------------------------------------------
def _write_sync(path: Path, content: str, create_dirs: bool) -> ToolResult:
    if path.is_dir():
        return ToolResult.error(f"{path} is a directory")
    if create_dirs:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult.error(f"cannot create {path.parent}: {exc}")
    elif not path.parent.exists():
        return ToolResult.error(f"no such directory: {path.parent} (pass create_dirs=true to create it)")
    try:
        data = content.encode("utf-8")
        path.write_bytes(data)
    except OSError as exc:
        return ToolResult.error(f"cannot write {path}: {exc}")
    return ToolResult(content=f"wrote {len(data)} bytes to {path.as_posix()}", meta={"path": str(path), "bytes": len(data)})


async def fs_write_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve(ctx, str(args["path"]))
    return await asyncio.to_thread(_write_sync, path, str(args.get("content") or ""), bool(args.get("create_dirs", True)))


# ---------------------------------------------------------------------------------
# fs_edit
# ---------------------------------------------------------------------------------
def _excerpt(before: str, after: str, path: str, width: int = 60) -> str:
    diff = difflib.unified_diff(
        before.splitlines(), after.splitlines(), fromfile=path, tofile=path, lineterm="", n=2
    )
    lines = list(diff)
    if len(lines) > width:
        lines = lines[:width] + [f"… ({len(lines) - width} more diff lines)"]
    return "\n".join(lines)


def _edit_sync(path: Path, old: str, new: str, replace_all: bool) -> ToolResult:
    if not path.exists():
        return ToolResult.error(f"no such file: {path}")
    if path.is_dir():
        return ToolResult.error(f"{path} is a directory")
    try:
        before = read_text(path)
    except OSError as exc:
        return ToolResult.error(f"cannot read {path}: {exc}")
    count = before.count(old)
    if count == 0:
        return ToolResult.error(f"`old` text not found in {path.as_posix()}; read the file and copy the text exactly")
    if count > 1 and not replace_all:
        return ToolResult.error(
            f"`old` text appears {count} times in {path.as_posix()}; include more surrounding "
            "context to make it unique, or pass replace_all=true"
        )
    after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
    replacements = count if replace_all else 1
    try:
        path.write_text(after, encoding="utf-8")
    except OSError as exc:
        return ToolResult.error(f"cannot write {path}: {exc}")
    return ToolResult(
        content=f"{replacements} replacement{'s' if replacements != 1 else ''} in {path.as_posix()}\n"
        + _excerpt(before, after, path.as_posix()),
        meta={"path": str(path), "replacements": replacements},
    )


async def fs_edit_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve(ctx, str(args["path"]))
    return await asyncio.to_thread(
        _edit_sync, path, str(args.get("old") or ""), str(args.get("new") or ""), bool(args.get("replace_all") or False)
    )


# ---------------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------------
def tools() -> list[Tool]:
    return [
        Tool(
            name="fs_read",
            description=prompts.load("tools/fs_read"),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to read."},
                    "offset": {"type": "integer", "description": "Zero-based first line.", "default": 0},
                    "limit": {"type": "integer", "description": "Lines to return.", "default": 400},
                },
                "required": ["path"],
            },
            handler=fs_read_handler,
            read_only=True,
            path_params=("path",),
        ),
        Tool(
            name="fs_list",
            description=prompts.load("tools/fs_list"),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to list."},
                    "pattern": {"type": "string", "description": "Glob relative to path.", "default": "**/*"},
                    "max_results": {"type": "integer", "default": 500},
                    "include_hidden": {"type": "boolean", "default": False},
                },
                "required": ["path"],
            },
            handler=fs_list_handler,
            read_only=True,
            path_params=("path",),
        ),
        Tool(
            name="fs_search",
            description=prompts.load("tools/fs_search"),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regular expression."},
                    "path": {"type": "string", "description": "File or directory to search."},
                    "glob": {"type": "string", "description": "Which files under path.", "default": "**/*"},
                    "context": {"type": "integer", "default": 2},
                    "max_results": {"type": "integer", "default": 100},
                    "case_sensitive": {"type": "boolean", "default": False},
                },
                "required": ["pattern", "path"],
            },
            handler=fs_search_handler,
            read_only=True,
            path_params=("path",),
        ),
        Tool(
            name="fs_write",
            description=prompts.load("tools/fs_write"),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to write."},
                    "content": {"type": "string", "description": "Whole new contents."},
                    "create_dirs": {"type": "boolean", "default": True},
                },
                "required": ["path", "content"],
            },
            handler=fs_write_handler,
            path_params=("path",),
        ),
        Tool(
            name="fs_edit",
            description=prompts.load("tools/fs_edit"),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to edit."},
                    "old": {"type": "string", "description": "Exact text to replace."},
                    "new": {"type": "string", "description": "Replacement text."},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["path", "old", "new"],
            },
            handler=fs_edit_handler,
            path_params=("path",),
        ),
    ]
