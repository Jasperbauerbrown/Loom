"""fs_patch: one unified diff, applied across files (audit #3).

The parser is hand-written on top of what `difflib` emits (`--- / +++ / @@` and the
` -+` line prefixes), because `difflib` writes diffs and does not read them. A hunk is
then applied by exactly the rule `fs_edit` uses: its context-plus-deletions block must
match the file exactly, and unambiguously. The stated line number is a hint that is
tried first, never a fuzz factor — nothing here guesses at text that does not match.

Every file in the patch is resolved, guarded (`fs_guard` asks about the paths inside the
diff through `register_paths`), checked against the stale-write ledger and computed in
memory; only when every file is ready is anything written.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...fs_guard import normalise
from ...registry import ToolContext, ToolResult
from . import ledger
from .common import TextError, TextFile, load_text, write_atomic

DEV_NULL = "/dev/null"


class PatchError(Exception):
    """A patch that cannot be parsed or cannot be applied. The message is what the
    model is told; nothing has been written when it is raised."""


@dataclass
class Hunk:
    old_start: int  # 1-based line in the original file, from the @@ header
    old: list[str] = field(default_factory=list)  # context + deletions
    new: list[str] = field(default_factory=list)  # context + additions
    header: str = ""

    @property
    def added(self) -> int:
        return max(0, len(self.new) - len(self.old))


@dataclass
class FilePatch:
    source: str | None  # None when the patch creates the file (--- /dev/null)
    target: str | None  # None when the patch deletes it (+++ /dev/null)
    hunks: list[Hunk] = field(default_factory=list)
    plus: int = 0
    minus: int = 0

    @property
    def action(self) -> str:
        if self.source is None:
            return "create"
        if self.target is None:
            return "delete"
        if self.target != self.source:
            return "rename"
        return "update"

    @property
    def path(self) -> str:
        """The path the result names: where the file ends up, or where it was."""
        return self.target or self.source or ""

    def paths(self) -> list[str]:
        return [p for p in (self.source, self.target) if p]


# ---------------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------------
def _strip_prefix(raw: str, strip: int | None) -> str | None:
    """`a/src/x.py` → `src/x.py`. `strip=None` is the default: drop a leading `a/` or
    `b/` (what git writes) and nothing else. `strip=N` drops N path components."""
    path = raw.split("\t")[0].strip().strip('"')
    if not path:
        return None
    if path == DEV_NULL or path.endswith("/dev/null"):
        return None
    path = path.replace("\\", "/")
    if strip is None:
        for prefix in ("a/", "b/"):
            if path.startswith(prefix):
                return path[len(prefix) :]
        return path
    parts = [p for p in path.split("/") if p not in ("", ".")]
    return "/".join(parts[strip:]) if len(parts) > strip else "/".join(parts[-1:])


def _hunk_start(header: str) -> int:
    """The `-start` of `@@ -start,count +start,count @@`."""
    try:
        old = header.split("@@")[1].strip().split(" ")[0]  # "-12,7"
        return int(old.lstrip("-").split(",")[0])
    except (IndexError, ValueError) as exc:
        raise PatchError(f"unreadable hunk header: {header.strip()!r}") from exc


def parse(text: str, strip: int | None = None) -> list[FilePatch]:
    """The patch as a list of per-file changes, in the order it names them."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        # The patch's own trailing newline, not an empty context line at the end of the
        # last hunk — left in, it became a phantom fourth line in every final hunk.
        lines.pop()
    files: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    pending_source: str | None = None
    saw_source = False
    for raw in lines:
        if raw.startswith("--- "):
            pending_source = _strip_prefix(raw[4:], strip)
            saw_source = True
            hunk = None
            continue
        if raw.startswith("+++ ") and saw_source:
            current = FilePatch(source=pending_source, target=_strip_prefix(raw[4:], strip))
            if current.source is None and current.target is None:
                raise PatchError("a file section names /dev/null on both sides")
            files.append(current)
            saw_source = False
            hunk = None
            continue
        if raw.startswith("@@"):
            if current is None:
                raise PatchError(f"hunk before any `--- `/`+++ ` file header: {raw.strip()!r}")
            hunk = Hunk(old_start=_hunk_start(raw), header=raw.strip())
            current.hunks.append(hunk)
            continue
        if hunk is None:
            continue  # `diff --git`, `index`, mode lines, prose around the patch
        if raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        if raw.startswith("+"):
            hunk.new.append(raw[1:])
            current.plus += 1 if current else 0
        elif raw.startswith("-"):
            hunk.old.append(raw[1:])
            current.minus += 1 if current else 0
        elif raw.startswith(" ") or raw == "":
            hunk.old.append(raw[1:] if raw else "")
            hunk.new.append(raw[1:] if raw else "")
        else:
            hunk = None  # trailing prose after the last hunk
    if not files:
        raise PatchError(
            "no file sections found; a patch is `--- a/path`, `+++ b/path`, then `@@` hunks "
            "with ' ', '-' and '+' line prefixes"
        )
    for file in files:
        if not file.hunks:
            raise PatchError(f"{file.path} has no @@ hunks")
    return files


def patch_paths(args: dict[str, Any]) -> list[str]:
    """Every path named inside the diff, for the filesystem guard. A patch that cannot
    be parsed yields nothing to guard — and nothing is written either, because the
    handler refuses it with the same parse error."""
    try:
        files = parse(str(args.get("patch") or ""), _strip_arg(args))
    except PatchError:
        return []
    out: list[str] = []
    for file in files:
        out += file.paths()
    return out


def _strip_arg(args: dict[str, Any]) -> int | None:
    value = args.get("strip")
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------
# applying
# ---------------------------------------------------------------------------------
def _locate(lines: list[str], old: list[str], hint: int) -> int:
    """Where this hunk's old block sits: the stated line when it matches there, else the
    one place it does match. Raises `PatchError` when it matches nowhere or twice."""
    span = len(old)
    if span == 0:
        return max(0, min(hint, len(lines)))
    if 0 <= hint <= len(lines) - span and lines[hint : hint + span] == old:
        return hint
    found = [i for i in range(0, len(lines) - span + 1) if lines[i : i + span] == old]
    if not found:
        raise PatchError(
            f"hunk at line {hint + 1} does not match the file; fs_read it and rebuild the patch "
            "against what is there now"
        )
    if len(found) > 1:
        raise PatchError(
            f"hunk at line {hint + 1} matches {len(found)} places and the line it names is not one of them; "
            "give the hunk more context"
        )
    return found[0]


def apply_hunks(text: str, hunks: list[Hunk], name: str) -> str:
    """Every hunk of one file, in order. Raises `PatchError` naming the file."""
    lines = text.split("\n")
    delta = 0
    for hunk in hunks:
        hint = max(0, hunk.old_start - 1 + delta)
        try:
            at = _locate(lines, hunk.old, hint)
        except PatchError as exc:
            raise PatchError(f"{name}: {exc}") from exc
        lines[at : at + len(hunk.old)] = hunk.new
        delta += len(hunk.new) - len(hunk.old)
    return "\n".join(lines)


@dataclass
class Planned:
    """One file's computed outcome, before anything is written."""

    file: FilePatch
    path: Path
    source_path: Path | None
    data: bytes | None  # None → delete
    summary: str


def _plan(file: FilePatch, resolve_path: Any) -> Planned:
    source = resolve_path(file.source) if file.source else None
    target = resolve_path(file.target) if file.target else source
    assert target is not None
    name = target.as_posix()
    if file.action == "create":
        if target.exists():
            raise PatchError(f"{name} already exists; a patch that creates a file must start from /dev/null")
        text = apply_hunks("", file.hunks, name)
        return Planned(file, target, None, TextFile(text="", newline="\n", bom=False).encode(text), f"created {name} (+{file.plus})")
    assert source is not None
    if not source.exists():
        raise PatchError(f"no such file: {source.as_posix()}")
    if source.is_dir():
        raise PatchError(f"{source.as_posix()} is a directory")
    try:
        tf = load_text(source)
    except TextError as exc:
        raise PatchError(str(exc)) from exc
    text = apply_hunks(tf.text, file.hunks, source.as_posix())
    if file.action == "delete":
        if text.strip():
            raise PatchError(
                f"{source.as_posix()}: a deletion patch must remove the whole file, "
                f"but {len(text.splitlines())} lines would be left"
            )
        return Planned(file, source, source, None, f"deleted {source.as_posix()}")
    if file.action == "rename":
        if target.exists():
            raise PatchError(f"{name} already exists; the patch renames {source.as_posix()} onto it")
        return Planned(file, target, source, tf.encode(text), f"renamed {source.as_posix()} → {name} (+{file.plus} -{file.minus})")
    return Planned(file, target, None, tf.encode(text), f"updated {name} ({len(file.hunks)} hunks, +{file.plus} -{file.minus})")


def _apply_sync(planned: list[Planned]) -> tuple[list[str], str | None]:
    """Write every planned file. Returns the paths written and the first failure."""
    done: list[str] = []
    for item in planned:
        try:
            if item.data is None:
                item.path.unlink()
            else:
                item.path.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(item.path, item.data)
                if item.source_path is not None and item.source_path != item.path:
                    item.source_path.unlink()
        except OSError as exc:
            return done, f"{item.path.as_posix()}: {exc}"
        done.append(item.path.as_posix())
    return done, None


async def fs_patch_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    base = args.get("cwd") if isinstance(args.get("cwd"), str) and args.get("cwd") else None
    roots = ctx.roots()
    base = normalise(base) if base else (roots[0] if roots else None)

    def resolve_path(raw: str) -> Path:
        return Path(normalise(raw, cwd=base))

    try:
        files = parse(str(args.get("patch") or ""), _strip_arg(args))
    except PatchError as exc:
        return ToolResult.error(str(exc))

    planned: list[Planned] = []
    try:
        for file in files:
            # The ledger first: a file that changed on disk must say so, not fail later
            # as a hunk that "does not match" — the model would rebuild the wrong thing.
            if file.source:
                stale = ledger.check(ctx.app, ctx.conversation_id, resolve_path(file.source))
                if stale:
                    return ToolResult.error(stale)
            planned.append(await asyncio.to_thread(_plan, file, resolve_path))
    except PatchError as exc:
        return ToolResult.error(str(exc))

    summary = "\n".join(p.summary for p in planned)
    if bool(args.get("dry_run") or False):
        return ToolResult(
            content=f"[dry run: the patch applies cleanly]\n{summary}",
            meta={"files": len(planned), "dry_run": True},
        )

    done, failure = await asyncio.to_thread(_apply_sync, planned)
    for item in planned:
        if item.path.as_posix() in done and item.data is not None:
            ledger.record(ctx.app, ctx.conversation_id, item.path, wrote=True)
    if failure:
        return ToolResult.error(
            f"the patch stopped part-way: {failure}. Applied: {', '.join(done) or 'nothing'}; "
            "fs_read the files and finish the change by hand"
        )
    hunks = sum(len(p.file.hunks) for p in planned)
    head = f"applied {hunks} hunk{'s' if hunks != 1 else ''} across {len(planned)} file{'s' if len(planned) != 1 else ''}"
    return ToolResult(content=f"{head}\n{summary}", meta={"files": len(planned), "hunks": hunks, "paths": done})
