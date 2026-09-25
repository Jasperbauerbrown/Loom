"""fs_read/fs_edit for PDFs (audit #16) and Jupyter notebooks (audit #17)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from loom.tools import fs_guard
from loom.tools.native.fs import notebook, pdf
from loom.tools.registry import ToolContext


@pytest.fixture
def project(app, tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    fs_guard.add_root(app, str(root), added_by="test")
    return root


@pytest.fixture
def ctx(app, project):
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1", event_id=1)


async def run(app, ctx, name, args):
    return await app.tools.get(name).handler(ctx, args)


# ---------------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------------
async def test_pdf_read_uses_pdftotext(app, ctx, project, monkeypatch):
    (project / "doc.pdf").write_bytes(b"%PDF-1.4\n...fake...")

    monkeypatch.setattr(pdf, "available", lambda: "/usr/bin/pdftotext")

    def fake_run(cmd, capture_output, timeout):
        assert cmd[0] == "/usr/bin/pdftotext"
        assert "-layout" in cmd
        assert str(project / "doc.pdf") in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=b"hello\nworld\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = await run(app, ctx, "fs_read", {"path": str(project / "doc.pdf")})
    assert result.ok
    assert "1\thello" in result.content
    assert "2\tworld" in result.content
    assert result.meta["kind"] == "pdf"


async def test_pdf_read_reports_missing_binary_clearly(app, ctx, project, monkeypatch):
    (project / "doc.pdf").write_bytes(b"%PDF-1.4\n...fake...")
    monkeypatch.setattr(pdf, "available", lambda: None)

    result = await run(app, ctx, "fs_read", {"path": str(project / "doc.pdf")})
    assert not result.ok
    assert "pdftotext" in result.content
    assert "poppler" in result.content.lower()


async def test_pdf_read_reports_nonzero_exit(app, ctx, project, monkeypatch):
    (project / "doc.pdf").write_bytes(b"%PDF-1.4\n...fake...")
    monkeypatch.setattr(pdf, "available", lambda: "/usr/bin/pdftotext")

    def fake_run(cmd, capture_output, timeout):
        return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"a bad PDF error")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = await run(app, ctx, "fs_read", {"path": str(project / "doc.pdf")})
    assert not result.ok
    assert "a bad PDF error" in result.content


def test_is_pdf_by_magic_bytes_and_suffix():
    assert pdf.is_pdf(Path("x.dat"), head=b"%PDF-1.7 rest")
    assert not pdf.is_pdf(Path("x.dat"), head=b"not a pdf")
    assert pdf.is_pdf(Path("x.pdf"))
    assert not pdf.is_pdf(Path("x.txt"))


# ---------------------------------------------------------------------------------
# notebooks: render
# ---------------------------------------------------------------------------------
def _sample_notebook() -> dict:
    return {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": ["# Title\n", "some text"],
            },
            {
                "cell_type": "code",
                "execution_count": 3,
                "metadata": {"tags": []},
                "outputs": [
                    {"output_type": "stream", "name": "stdout", "text": ["a\n", "b\n", "c\n"]},
                    {
                        "output_type": "execute_result",
                        "execution_count": 3,
                        "data": {"text/plain": ["1\n", "2\n", "3\n"]},
                        "metadata": {},
                    },
                    {
                        "output_type": "display_data",
                        "data": {"image/png": "base64garbage=="},
                        "metadata": {},
                    },
                    {
                        "output_type": "error",
                        "ename": "ValueError",
                        "evalue": "boom",
                        "traceback": ["long traceback line 1", "long traceback line 2"],
                    },
                ],
                "source": "print(1)\nprint(2)",
            },
        ],
        "metadata": {"kernelspec": {"name": "python3"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def test_notebook_render_headers_and_summarised_outputs():
    raw = json.dumps(_sample_notebook())
    text = notebook.render(raw)

    assert "[cell 0] markdown" in text
    assert "# Title" in text
    assert "[cell 1] code [3]" in text
    assert "print(1)" in text

    assert "[out] stream stdout: 3 lines" in text
    assert "[out] execute_result text/plain: 3 lines" in text
    assert "[out] display_data: image/png" in text
    assert "[out] error: ValueError: boom" in text

    # Never dumped: no base64 blob or raw traceback lines in the rendered text.
    assert "base64garbage" not in text
    assert "long traceback line" not in text


def test_notebook_render_rejects_bad_json():
    with pytest.raises(notebook.NotebookError):
        notebook.render("not json")
    with pytest.raises(notebook.NotebookError):
        notebook.render(json.dumps({"no_cells": []}))


async def test_notebook_read_pages_through_fs_read(app, ctx, project):
    nb = _sample_notebook()
    (project / "nb.ipynb").write_text(json.dumps(nb, indent=1), encoding="utf-8")
    result = await run(app, ctx, "fs_read", {"path": str(project / "nb.ipynb")})
    assert result.ok
    assert result.meta["kind"] == "notebook"
    assert result.meta["cells"] == 2
    assert "[cell 0] markdown" in result.content


# ---------------------------------------------------------------------------------
# notebooks: apply_edits
# ---------------------------------------------------------------------------------
def test_apply_edits_preserves_everything_else():
    nb = _sample_notebook()
    raw = json.dumps(nb, indent=1)

    from loom.tools.native.fs.common import Edit

    new_raw, count = notebook.apply_edits(raw, [Edit(old="print(1)", new="print('one')")])
    assert count == 1

    new_nb = json.loads(new_raw)
    # unrelated content untouched
    assert new_nb["metadata"] == nb["metadata"]
    assert new_nb["cells"][1]["outputs"] == nb["cells"][1]["outputs"]
    assert new_nb["cells"][1]["execution_count"] == 3
    assert new_nb["cells"][0]["source"] == nb["cells"][0]["source"]

    # source-list shape preserved: cell 1's source was a plain string
    assert isinstance(new_nb["cells"][1]["source"], str)
    assert new_nb["cells"][1]["source"] == "print('one')\nprint(2)"

    # indentation preserved (nbformat's own indent=1)
    assert new_raw.startswith("{\n \"cells\"") or new_raw.splitlines()[1].startswith(" \"")
    assert not new_raw.endswith("\n")  # json.dumps produced no trailing newline; raw had none


def test_apply_edits_preserves_list_source_shape():
    nb = _sample_notebook()
    raw = json.dumps(nb, indent=1)

    from loom.tools.native.fs.common import Edit

    new_raw, count = notebook.apply_edits(raw, [Edit(old="# Title", new="# New Title")])
    assert count == 1
    new_cell0 = json.loads(new_raw)["cells"][0]
    assert isinstance(new_cell0["source"], list)
    assert new_cell0["source"][0] == "# New Title\n"
    assert new_cell0["source"][1] == "some text"


def test_apply_edits_ambiguous_without_replace_all():
    nb = {
        "cells": [
            {"cell_type": "code", "source": "x = 1", "execution_count": None, "outputs": []},
            {"cell_type": "code", "source": "x = 1", "execution_count": None, "outputs": []},
        ]
    }
    raw = json.dumps(nb)
    from loom.tools.native.fs.common import Edit, EditError

    with pytest.raises(EditError):
        notebook.apply_edits(raw, [Edit(old="x = 1", new="x = 2")])

    new_raw, count = notebook.apply_edits(raw, [Edit(old="x = 1", new="x = 2", replace_all=True)])
    assert count == 2


async def test_notebook_edit_through_fs_edit_handler(app, ctx, project):
    nb = _sample_notebook()
    path = project / "nb.ipynb"
    path.write_text(json.dumps(nb, indent=1), encoding="utf-8")

    # The stale-write guard requires a read first, in this same ctx.
    await run(app, ctx, "fs_read", {"path": str(path)})

    result = await run(app, ctx, "fs_edit", {"path": str(path), "old": "print(1)", "new": "print('one')"})
    assert result.ok, result.content
    assert result.meta["replacements"] == 1

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["cells"][1]["source"] == "print('one')\nprint(2)"
    assert saved["cells"][1]["outputs"] == nb["cells"][1]["outputs"]
