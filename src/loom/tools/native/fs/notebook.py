"""Jupyter notebooks for fs_read and fs_edit (audit #17).

A `.ipynb` is JSON, so there is no dependency here: reading renders the cells as text a
model can act on (outputs summarised, never dumped), and editing matches `old`/`new`
against cell *sources* and rewrites the JSON with everything else — outputs, metadata,
execution counts, the file's own indentation — left as it was.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

from .common import Edit, EditError, edit_problem, find_spans, replace_spans

DEFAULT_INDENT = 1
_TEXT_MIME_PREFIXES = ("text/", "application/json")


class NotebookError(Exception):
    """An expected failure; the message becomes the tool error verbatim."""


def is_notebook(path: Path) -> bool:
    return path.suffix.lower() == ".ipynb"


def _join_source(source: Any) -> str:
    """A cell's `source` (list of lines or a plain string) as one string."""
    if isinstance(source, list):
        return "".join(str(line) for line in source)
    return str(source or "")


def _count_lines(text: str) -> int:
    return len(text.splitlines()) if text else 0


def _summarize_output(output: dict) -> list[str]:
    """One or more indented `[out] ...` lines: summarised, never dumped."""
    out_type = output.get("output_type")
    if out_type == "stream":
        name = output.get("name", "stdout")
        text = _join_source(output.get("text", ""))
        return [f"  [out] stream {name}: {_count_lines(text)} lines"]
    if out_type in ("execute_result", "display_data"):
        data = output.get("data", {})
        if not isinstance(data, dict) or not data:
            return [f"  [out] {out_type}"]
        lines = []
        for mime, value in data.items():
            if mime.startswith(_TEXT_MIME_PREFIXES):
                text = _join_source(value)
                lines.append(f"  [out] {out_type} {mime}: {_count_lines(text)} lines")
            else:
                lines.append(f"  [out] {out_type}: {mime}")
        return lines
    if out_type == "error":
        ename = output.get("ename", "Error")
        evalue = output.get("evalue", "")
        return [f"  [out] error: {ename}: {evalue}"]
    return [f"  [out] {out_type or 'output'}"]


def _load(raw: str) -> dict:
    try:
        nb = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NotebookError(f"not valid JSON: {exc}") from exc
    cells = nb.get("cells") if isinstance(nb, dict) else None
    if not isinstance(cells, list):
        raise NotebookError("not a notebook: no `cells` list")
    return nb


def render(raw: str) -> str:
    """The notebook as readable text: one block per cell, `[cell N] <type>` then the
    source, then a one-line summary per output. Raises `NotebookError` on bad JSON."""
    nb = _load(raw)
    blocks: list[str] = []
    for i, cell in enumerate(nb["cells"]):
        if not isinstance(cell, dict):
            cell = {}
        cell_type = cell.get("cell_type", "code")
        source = _join_source(cell.get("source", ""))
        header = f"[cell {i}] {cell_type}"
        if cell_type == "code":
            count = cell.get("execution_count")
            header += f" [{count if count is not None else ' '}]"
        block = [header, source]
        if cell_type == "code":
            for output in cell.get("outputs") or []:
                if isinstance(output, dict):
                    block.extend(_summarize_output(output))
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def cell_count(raw: str) -> int:
    """The number of cells, for the read meta. Raises `NotebookError` on bad JSON."""
    return len(_load(raw)["cells"])


def _detect_indent(raw: str) -> int:
    """The indentation `raw` was written with (nbformat's own writer uses `indent=1`),
    from the first indented line; falls back to the nbformat default."""
    match = re.search(r"\n( +)\S", raw)
    return len(match.group(1)) if match else DEFAULT_INDENT


def _set_source(cell: dict, new_source: str) -> None:
    """Keep the cell's `source` shape: a list of lines stays a list, a string stays a
    string, and list lines keep their trailing `\\n` (the last usually without)."""
    if isinstance(cell.get("source"), list):
        cell["source"] = new_source.splitlines(keepends=True)
    else:
        cell["source"] = new_source


def apply_edits(raw: str, edits: Sequence[Edit]) -> tuple[str, int]:
    """Apply `edits` to the cell sources in order and return `(new_raw_json, count)`.

    Matching is the one rule from `common.apply_edit`, counted across all cell sources
    together: the text must occur exactly once unless the edit says `replace_all`.
    Raises `common.EditError` when an edit does not apply and `NotebookError` when the
    file is not a notebook. Everything the JSON holds beyond the edited sources is
    preserved, including the file's indentation and its trailing newline.
    """
    nb = _load(raw)
    cells = nb["cells"]
    total = 0
    for edit in edits:
        if not edit.old:
            raise EditError("`old` is empty; it must be the exact text to replace")
        sources = [_join_source(c.get("source", "")) if isinstance(c, dict) else "" for c in cells]
        spans_per_cell = [find_spans(s, edit.old) for s in sources]
        count = sum(len(spans) for spans in spans_per_cell)
        problem = edit_problem(count, edit.replace_all, "the notebook's cells")
        if problem:
            raise EditError(problem)
        new = edit.new.replace("\r\n", "\n")
        made = 0
        for cell, source, spans in zip(cells, sources, spans_per_cell):
            if not isinstance(cell, dict) or not spans:
                continue
            if edit.replace_all:
                _set_source(cell, replace_spans(source, spans, new))
                made += len(spans)
            else:
                _set_source(cell, replace_spans(source, spans[:1], new))
                made += 1
                break
        total += made
    indent = _detect_indent(raw)
    new_raw = json.dumps(nb, indent=indent, ensure_ascii=False)
    if raw.endswith("\n"):
        new_raw += "\n"
    return new_raw, total
