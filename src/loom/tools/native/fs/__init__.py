"""Filesystem tools: fs_read, fs_list, fs_search, fs_write, fs_edit, fs_patch (03 §9.2).

All six take paths the guard has already resolved against the allowed roots; the
handlers do the work and report it in a shape a model can act on without guessing —
a footer that names the next offset rather than a bare truncation, a match count rather
than "not found", a diff rather than "ok".

The package is one module per job: `read` (text, PDF, notebooks, images), `search`
(fs_list, fs_search), `write` (fs_write, fs_edit), `patch` (unified diffs), `ledger`
(the per-conversation stale-write ledger), `notebook`, `pdf`, and `common` for the
plumbing they share. The schemas the model sees live here, in one place.

Blocking file work runs in a worker thread so one large search cannot stall the event
loop and every other conversation with it.
"""

from __future__ import annotations

from .... import prompts
from ...fs_guard import register_paths
from ...registry import Tool
from . import ledger, patch, read, search, write

# Re-exported for the tests and for other packages that reason about file reads.
__all__ = ["tools", "ledger", "patch", "read", "search", "write"]


def tools() -> list[Tool]:
    # The guard cannot read the paths inside a unified diff from `path_params`, so
    # fs_patch hands it an extractor (the one mechanism, registered in one place).
    register_paths("fs_patch", patch.patch_paths)
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
                    "line_numbers": {
                        "type": "boolean",
                        "description": "Prefix each line with its number and a tab.",
                        "default": True,
                    },
                    "layout": {
                        "type": "boolean",
                        "description": "PDFs only: keep the page layout (pdftotext -layout).",
                        "default": True,
                    },
                },
                "required": ["path"],
            },
            handler=read.fs_read_handler,
            read_only=True,
            path_params=("path",),
            paged=True,
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
            handler=search.fs_list_handler,
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
            handler=search.fs_search_handler,
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
            handler=write.fs_write_handler,
            path_params=("path",),
        ),
        Tool(
            name="fs_edit",
            description=prompts.load("tools/fs_edit"),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to edit."},
                    "old": {"type": "string", "description": "Exact text to replace (one edit)."},
                    "new": {"type": "string", "description": "Replacement text (one edit)."},
                    "replace_all": {"type": "boolean", "default": False},
                    "edits": {
                        "type": "array",
                        "description": "Several edits, applied in order, all or none. Use instead of old/new.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old": {"type": "string"},
                                "new": {"type": "string"},
                                "replace_all": {"type": "boolean", "default": False},
                            },
                            "required": ["old", "new"],
                        },
                    },
                },
                "required": ["path"],
            },
            handler=write.fs_edit_handler,
            path_params=("path",),
        ),
        Tool(
            name="fs_patch",
            description=prompts.load("tools/fs_patch"),
            parameters={
                "type": "object",
                "properties": {
                    "patch": {"type": "string", "description": "A unified diff, one or more file sections."},
                    "cwd": {"type": "string", "description": "Directory the patch's relative paths resolve against."},
                    "strip": {
                        "type": "integer",
                        "description": "Path components to drop from each path (like patch -pN). Default: drop a leading a/ or b/.",
                    },
                    "dry_run": {"type": "boolean", "description": "Check that it applies; write nothing.", "default": False},
                },
                "required": ["patch"],
            },
            handler=patch.fs_patch_handler,
            path_params=("cwd",),
        ),
    ]
