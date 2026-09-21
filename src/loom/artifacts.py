"""Artifact spill and recall (03 §4.4).

Tool output longer than `conversation.tool_output_cap` is written whole to
`artifacts/<conversation>/<event>.txt` and replaced in the transcript by head + tail +
a pointer line. The full text stays reachable through `artifact_recall(id, offset,
limit)` for as long as the conversation exists; archived conversations' artifacts are
pruned by maintenance after `conversation.artifact_retention_days`.

`bound` is the only entry point the dispatcher uses: it decides, spills, and returns the
text to persist.

Images (2026-09-21) live beside the spills as `artifacts/<conversation>/img_<ulid><ext>`
with no table row: the transcript keeps only `{"id", "mime"}` refs on event meta, data
URLs are built at context-assembly time, and every chars/4 estimate stays blind to
base64. Write-before-reference: the file lands before any event points at it.
"""

from __future__ import annotations

import base64
import datetime as _dt
import logging
import re
from pathlib import Path
from typing import Any

from . import db
from .ids import ulid

log = logging.getLogger(__name__)

ELLIPSIS = "\n…\n"

IMAGE_MIMES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
_EXT_TO_MIME = {ext: mime for mime, ext in IMAGE_MIMES.items()}
# `img_` + a 26-char Crockford ULID: the strict shape doubles as the traversal guard.
IMAGE_ID_RE = re.compile(r"^img_[0-9A-HJKMNP-TV-Z]{26}$")


def sniff_image(data: bytes) -> str | None:
    """The mime of a supported image by its magic bytes, else None."""
    head = bytes(data[:16])
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) for PNG, JPEG and GIF from their headers; None when unknown."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        if data[:3] == b"\xff\xd8\xff":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = int.from_bytes(data[i + 2 : i + 4], "big")
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    return int.from_bytes(data[i + 7 : i + 9], "big"), int.from_bytes(data[i + 5 : i + 7], "big")
                i += 2 + length
    except Exception:  # a malformed header is not worth a crash: dimensions are decoration
        return None
    return None


class Store:
    """`app.artifacts`."""

    def __init__(self, app: Any):
        self.app = app

    @property
    def root(self) -> Path:
        return Path(self.app.artifacts_dir)

    # -- write ------------------------------------------------------------------
    def spill(self, conversation_id: str, event_id: int | None, text: str) -> dict[str, Any]:
        """Write `text` to disk, insert the row, return it. Persist before act: the file
        lands first, then the row that points at it."""
        artifact_id = ulid()
        folder = self.root / conversation_id
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{event_id}.txt" if event_id is not None else f"{artifact_id}.txt"
        path = folder / name
        if path.exists():  # several calls in one tool_call event
            path = folder / f"{artifact_id}.txt"
        path.write_text(text, encoding="utf-8")

        head_n = int(self.app.settings.get("conversation.artifact_head_chars"))
        tail_n = int(self.app.settings.get("conversation.artifact_tail_chars"))
        row = {
            "id": artifact_id,
            "conversation_id": conversation_id,
            "event_id": event_id,
            "path": str(path).replace("\\", "/"),
            "bytes": len(text.encode("utf-8")),
            "head": text[:head_n],
            "tail": text[-tail_n:] if tail_n else "",
            "created_at": db.now_iso(),
        }
        self.app.db.execute(
            "INSERT INTO artifacts(id, conversation_id, event_id, path, bytes, head, tail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["conversation_id"], row["event_id"], row["path"],
                row["bytes"], row["head"], row["tail"], row["created_at"],
            ),
        )
        return row

    def bound(self, conversation_id: str, event_id: int | None, text: str) -> tuple[str, str | None]:
        """(text_to_persist, artifact_id_or_None). Under the cap nothing is written."""
        text = text if isinstance(text, str) else str(text)
        cap = int(self.app.settings.get("conversation.tool_output_cap"))
        if len(text) <= cap:
            return text, None
        try:
            row = self.spill(conversation_id, event_id, text)
        except OSError as exc:  # disk full, bad path: truncate visibly rather than fail
            log.warning("artifact spill failed (%s); truncating instead", exc)
            return text[:cap] + f"\n[output truncated at {cap} chars; artifact spill failed: {exc}]", None
        pointer = (
            f"[artifact {row['id']}: {len(text)} chars — "
            f"artifact_recall(id=\"{row['id']}\", offset, limit) for more]"
        )
        return f"{row['head']}{ELLIPSIS}{row['tail']}\n{pointer}", row["id"]

    # -- images -----------------------------------------------------------------
    def image_cap_bytes(self) -> int:
        return int(self.app.settings.get("conversation.image_max_mb")) * 1024 * 1024

    def _image_folder(self, conversation_id: str) -> Path:
        cid = str(conversation_id or "")
        if not cid or "/" in cid or "\\" in cid or cid in (".", ".."):
            raise ValueError(f"invalid conversation id {conversation_id!r}")
        return self.root / cid

    def store_image(self, conversation_id: str, data: bytes, mime: str) -> dict[str, Any]:
        """Persist image bytes; returns `{"id", "mime", "bytes"}`. `ValueError` for an
        unsupported type or a size over `conversation.image_max_mb`. The file exists
        before the caller writes any event that refers to it."""
        ext = IMAGE_MIMES.get(str(mime or "").lower())
        if ext is None:
            raise ValueError(f"unsupported image type {mime!r} (supported: {', '.join(sorted(IMAGE_MIMES))})")
        cap = self.image_cap_bytes()
        if len(data) > cap:
            raise ValueError(
                f"image is {len(data)} bytes, over the {cap // (1024 * 1024)} MB cap (conversation.image_max_mb)"
            )
        folder = self._image_folder(conversation_id)
        folder.mkdir(parents=True, exist_ok=True)
        image_id = f"img_{ulid()}"
        (folder / f"{image_id}{ext}").write_bytes(bytes(data))
        return {"id": image_id, "mime": str(mime).lower(), "bytes": len(data)}

    def find_image(self, conversation_id: str, image_id: str) -> tuple[Path, str] | None:
        """`(path, mime)` for a stored image, or None for a bad id shape or a missing file."""
        if not IMAGE_ID_RE.match(str(image_id or "")):
            return None
        try:
            folder = self._image_folder(conversation_id)
        except ValueError:
            return None
        for ext, mime in _EXT_TO_MIME.items():
            path = folder / f"{image_id}{ext}"
            if path.is_file():
                return path, mime
        return None

    def read_image(self, conversation_id: str, image_id: str) -> tuple[bytes, str] | None:
        found = self.find_image(conversation_id, image_id)
        if found is None:
            return None
        path, mime = found
        try:
            return path.read_bytes(), mime
        except OSError as exc:
            log.warning("image %s unreadable: %s", image_id, exc)
            return None

    def image_data_url(self, conversation_id: str, image_id: str) -> str | None:
        """The model-facing wire form: `data:<mime>;base64,…`, or None on any miss."""
        got = self.read_image(conversation_id, image_id)
        if got is None:
            return None
        data, mime = got
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    # -- read -------------------------------------------------------------------
    def get(self, artifact_id: str) -> dict[str, Any] | None:
        return db.row_to_dict(
            self.app.db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        )

    def list(self, conversation_id: str) -> list[dict[str, Any]]:
        rows = self.app.db.execute(
            "SELECT * FROM artifacts WHERE conversation_id=? ORDER BY id", (conversation_id,)
        ).fetchall()
        return [db.row_to_dict(r) for r in rows]  # type: ignore[misc]

    def text(self, artifact_id: str) -> str | None:
        row = self.get(artifact_id)
        if row is None:
            return None
        try:
            return Path(row["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("artifact %s unreadable: %s", artifact_id, exc)
            return None

    def read(self, artifact_id: str, offset: int = 0, limit: int = 8000) -> str:
        """A character slice with a paging footer. Missing artifact → an error string;
        the tool turns it into an error result."""
        row = self.get(artifact_id)
        if row is None:
            return f"error: no artifact {artifact_id}"
        content = self.text(artifact_id)
        if content is None:
            return f"error: artifact {artifact_id} file is missing ({row['path']})"
        total = len(content)
        offset = max(0, int(offset))
        limit = max(1, int(limit))
        chunk = content[offset : offset + limit]
        end = offset + len(chunk)
        if offset >= total and total:
            return f"[artifact {artifact_id}: offset {offset} is past the end ({total} chars)]"
        if end < total:
            chunk += (
                f"\n[chars {offset}–{end} of {total}; "
                f"artifact_recall(id=\"{artifact_id}\", offset={end}) for more]"
            )
        return chunk

    # -- maintenance ------------------------------------------------------------
    def prune_archived(self, retention_days: int | None = None) -> int:
        """Delete files and rows for artifacts of archived conversations whose
        `archived_at` is older than the retention window. Returns the count removed."""
        if retention_days is None:
            retention_days = int(self.app.settings.get("conversation.artifact_retention_days"))
        cutoff = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=int(retention_days))
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        rows = self.app.db.execute(
            "SELECT a.id, a.path FROM artifacts a JOIN conversations c ON c.id = a.conversation_id "
            "WHERE c.status='archived' AND c.archived_at IS NOT NULL AND c.archived_at < ?",
            (cutoff,),
        ).fetchall()
        removed = 0
        for row in rows:
            try:
                Path(row["path"]).unlink(missing_ok=True)
            except OSError as exc:
                log.warning("artifact %s: unlink failed (%s); dropping the row anyway", row["id"], exc)
            self.app.db.execute("DELETE FROM artifacts WHERE id=?", (row["id"],))
            removed += 1
        # Images have no rows: sweep the same conversations' folders by name.
        stale = self.app.db.execute(
            "SELECT id FROM conversations WHERE status='archived' AND archived_at IS NOT NULL AND archived_at < ?",
            (cutoff,),
        ).fetchall()
        for conv in stale:
            folder = self.root / str(conv["id"])
            if not folder.is_dir():
                continue
            for path in folder.glob("img_*"):
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    log.warning("image %s: unlink failed (%s)", path.name, exc)
        return removed


def install(app: Any) -> None:
    app.artifacts = Store(app)
    Path(app.artifacts_dir).mkdir(parents=True, exist_ok=True)
