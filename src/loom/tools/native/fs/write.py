"""fs_write and fs_edit: the two ways a model changes one file.

Both go through the same three gates before anything is written:

    F9   a relative path that names an existing vault note is a mistake, not a new file
    #2   the stale-write guard: this conversation must have seen the file as it is now
    #18  the file's line endings and BOM are read before the write and put back after

`fs_edit` takes either one `old`/`new` pair or a list of `edits`; a list is applied in
order to the text in memory and written once, so a failing edit leaves the file exactly
as it was and the result carries a single diff of the whole change (audit #3).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ...registry import ToolContext, ToolResult
from . import ledger, notebook
from .common import (
    Edit,
    EditError,
    TextError,
    TextFile,
    apply_edit,
    load_text,
    resolve,
    style_of,
    unified_excerpt,
    vault_refusal,
    with_base_hint,
    write_atomic,
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
    existed = path.exists()
    newline, bom = style_of(path) if existed else ("\n", False)
    try:
        data = TextFile(text="", newline=newline, bom=bom).encode(content)
        write_atomic(path, data)
    except OSError as exc:
        return ToolResult.error(f"cannot write {path}: {exc}")
    note = ""
    if existed and (newline == "\r\n" or bom):
        kept = ["CRLF line endings"] if newline == "\r\n" else []
        kept += ["a BOM"] if bom else []
        note = f" (kept {' and '.join(kept)})"
    return ToolResult(
        content=f"wrote {len(data)} bytes to {path.as_posix()}{note}",
        meta={"path": str(path), "bytes": len(data), "created": not existed},
    )


async def fs_write_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    raw = str(args["path"])
    path = resolve(ctx, raw)
    refusal = vault_refusal(ctx, raw, path)
    if refusal is not None:
        return refusal
    stale = ledger.check(ctx.app, ctx.conversation_id, path)
    if stale:
        return ToolResult.error(stale)
    result = await asyncio.to_thread(_write_sync, path, str(args.get("content") or ""), bool(args.get("create_dirs", True)))
    if result.ok:
        ledger.record(ctx.app, ctx.conversation_id, path, wrote=True)
    return with_base_hint(ctx, raw, result)


# ---------------------------------------------------------------------------------
# fs_edit
# ---------------------------------------------------------------------------------
def parse_edits(args: dict[str, Any]) -> list[Edit]:
    """`old`/`new` or `edits=[…]`, never both. Raises `EditError` with what to resend."""
    listed = args.get("edits")
    single = args.get("old") is not None or args.get("new") is not None
    if listed is not None and single:
        return _fail("pass either `old`/`new` or `edits`, not both")
    if listed is None:
        if not single:
            return _fail("nothing to do: pass `old` and `new`, or a list of `edits`")
        return [Edit(str(args.get("old") or ""), str(args.get("new") or ""), bool(args.get("replace_all") or False))]
    if not isinstance(listed, (list, tuple)) or not listed:
        return _fail("`edits` must be a non-empty list of {old, new, replace_all?} objects")
    out: list[Edit] = []
    for i, item in enumerate(listed):
        if not isinstance(item, dict):
            return _fail(f"edit {i + 1} is a {type(item).__name__}, not an object with `old` and `new`")
        if item.get("old") is None:
            return _fail(f"edit {i + 1} has no `old`")
        out.append(Edit(str(item.get("old") or ""), str(item.get("new") or ""), bool(item.get("replace_all") or False)))
    return out


def _fail(message: str) -> list[Edit]:
    raise EditError(message)


def _where(path: Path, index: int, total: int) -> str:
    return f"{path.as_posix()}" if total == 1 else f"{path.as_posix()} (edit {index + 1} of {total})"


def _edit_text(path: Path, edits: list[Edit]) -> tuple[str, str, int]:
    """`(before, after, replacements)` for a text file. Raises `EditError`/`TextError`."""
    tf = load_text(path)
    before = tf.text
    after = before
    count = 0
    for i, edit in enumerate(edits):
        after, made = apply_edit(after, edit, _where(path, i, len(edits)))
        count += made
    write_atomic(path, tf.encode(after))
    return before, after, count


def _edit_notebook(path: Path, edits: list[Edit]) -> tuple[str, str, int]:
    """The same, for a notebook: `old`/`new` match cell sources and the JSON is rewritten
    around them (audit #17)."""
    tf = load_text(path)
    new_raw, count = notebook.apply_edits(tf.text, edits)
    before = notebook.render(tf.text)
    after = notebook.render(new_raw)
    write_atomic(path, tf.encode(new_raw))
    return before, after, count


def _edit_sync(path: Path, edits: list[Edit]) -> ToolResult:
    if not path.exists():
        return ToolResult.error(f"no such file: {path}")
    if path.is_dir():
        return ToolResult.error(f"{path} is a directory")
    try:
        is_nb = notebook.is_notebook(path)
    except Exception:
        is_nb = False
    try:
        before, after, count = (_edit_notebook if is_nb else _edit_text)(path, edits)
    except (EditError, TextError, notebook.NotebookError) as exc:
        return ToolResult.error(str(exc))
    except OSError as exc:
        return ToolResult.error(f"cannot write {path}: {exc}")
    plural = "s" if count != 1 else ""
    head = f"{count} replacement{plural} in {path.as_posix()}"
    if len(edits) > 1:
        head += f" ({len(edits)} edits, applied in order)"
    return ToolResult(
        content=head + "\n" + unified_excerpt(before, after, path.as_posix()),
        meta={"path": str(path), "replacements": count, "edits": len(edits)},
    )


async def fs_edit_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    raw = str(args["path"])
    path = resolve(ctx, raw)
    try:
        edits = parse_edits(args)
    except EditError as exc:
        return ToolResult.error(str(exc))
    refusal = vault_refusal(ctx, raw, path)
    if refusal is not None:
        return refusal
    stale = ledger.check(ctx.app, ctx.conversation_id, path)
    if stale:
        return ToolResult.error(stale)
    result = await asyncio.to_thread(_edit_sync, path, edits)
    if result.ok:
        ledger.record(ctx.app, ctx.conversation_id, path, wrote=True)
    return with_base_hint(ctx, raw, result)
