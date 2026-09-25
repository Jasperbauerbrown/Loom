"""PDF text for fs_read, through `pdftotext` when it is on PATH (audit #16).

No new Python dependency (charter: dependency-light): the binary does the work, or
fs_read says which binary is missing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

BINARY = "pdftotext"
TIMEOUT_S = 60.0
STDERR_TAIL = 500


class PdfError(Exception):
    """An expected failure; the message becomes the tool error verbatim."""


def is_pdf(path: Path, head: bytes = b"") -> bool:
    """True for a PDF, by magic bytes when we have them, else by suffix."""
    if head:
        return head.startswith(b"%PDF-")
    return path.suffix.lower() == ".pdf"


def available() -> str | None:
    """The `pdftotext` on PATH, or None."""
    return shutil.which(BINARY)


def extract(path: Path, layout: bool = True) -> str:
    """The PDF's text. Raises `PdfError` when the binary is missing or fails."""
    binary = available()
    if not binary:
        raise PdfError(
            f"{BINARY} is not on PATH; install poppler-utils (or the Xpdf command-line "
            "tools) to read PDFs — fs_read uses no Python PDF dependency"
        )
    cmd = [binary]
    if layout:
        cmd.append("-layout")
    cmd += ["--", str(path), "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise PdfError(f"{BINARY} timed out after {TIMEOUT_S:.0f}s reading {path}") from exc
    except OSError as exc:
        raise PdfError(f"cannot run {BINARY}: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        tail = stderr[-STDERR_TAIL:] if stderr else "(no stderr)"
        raise PdfError(f"{BINARY} exited {proc.returncode} reading {path}: {tail}")
    return proc.stdout.decode("utf-8", errors="replace")
