"""Artifact spill and recall (03 §4.4).

Tool output longer than `conversation.tool_output_cap` is written whole to
`artifacts/<conversation>/<event>.txt` and replaced in the transcript by head + tail +
a pointer line. The full text stays reachable through `artifact_recall(id, offset,
limit)` for as long as the conversation exists; archived conversations' artifacts are
pruned by maintenance after `conversation.artifact_retention_days`.

`bound` is the only entry point the dispatcher uses: it decides, spills, and returns the
text to persist.
"""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path
from typing import Any

from . import db
from .ids import ulid

log = logging.getLogger(__name__)

ELLIPSIS = "\n…\n"


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
        return removed


def install(app: Any) -> None:
    app.artifacts = Store(app)
    Path(app.artifacts_dir).mkdir(parents=True, exist_ok=True)
