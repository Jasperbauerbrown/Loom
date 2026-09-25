"""The per-conversation file ledger behind the stale-write guard (audit #2).

One row per (conversation, file): the mtime and size this conversation last saw. Every
fs tool that reads records what it saw; every fs tool that writes checks first and
records what it left behind. Four classes, one behaviour each:

    record matches the file        → proceed
    record differs from the file   → refuse: it changed on disk, read it again
    no record and the file exists  → refuse: read it first
    the file does not exist        → proceed (fs_write and a patch's `+++` create it)

The row is in SQLite, not in memory, so a backend restart does not turn every next write
into a refusal. `wrote` marks the rows a writing tool left, which is how another part of
the harness learns which files this conversation wrote.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .... import db

log = logging.getLogger(__name__)


def stamp(path: Path) -> tuple[int, int] | None:
    """`(mtime_ns, size)` of the file, or None when it is not there (or unreadable)."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (int(st.st_mtime_ns), int(st.st_size))


def _key(path: Path) -> str:
    from ...fs_guard import normalise

    return normalise(str(path))


def row(app: Any, conversation_id: str, path: Path) -> dict[str, Any] | None:
    found = app.db.execute(
        "SELECT * FROM file_reads WHERE conversation_id=? AND path=?",
        (conversation_id, _key(path)),
    ).fetchone()
    return dict(found) if found else None


def record(app: Any, conversation_id: str, path: Path, wrote: bool = False) -> None:
    """Remember what this conversation now sees of `path`. A file that vanished between
    the call and this line leaves no row — the next write then asks for a read."""
    current = stamp(path)
    if current is None:
        return
    mtime_ns, size = current
    try:
        app.db.execute(
            "INSERT INTO file_reads(conversation_id, path, mtime_ns, size, seen_at, wrote) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(conversation_id, path) DO UPDATE SET "
            "mtime_ns=excluded.mtime_ns, size=excluded.size, seen_at=excluded.seen_at, "
            "wrote=MAX(file_reads.wrote, excluded.wrote)",
            (conversation_id, _key(path), mtime_ns, size, db.now_iso(), 1 if wrote else 0),
        )
    except Exception as exc:  # the ledger must never cost a conversation its tool call
        log.warning("file ledger: could not record %s: %r", path, exc)


def check(app: Any, conversation_id: str, path: Path) -> str | None:
    """None when the write may proceed, else the refusal to hand the model."""
    current = stamp(path)
    if current is None:
        return None  # nothing there yet: creating it is the whole point
    try:
        known = row(app, conversation_id, path)
    except Exception as exc:
        log.warning("file ledger: could not read %s: %r", path, exc)
        return None
    name = Path(path).as_posix()
    if known is None:
        return (
            f"{name} exists and this conversation has not read it; fs_read it first, then write — "
            "so an edit cannot clobber work nobody here has seen"
        )
    mtime_ns, size = current
    if int(known["mtime_ns"]) == mtime_ns and int(known["size"]) == size:
        return None
    return (
        f"{name} changed on disk since this conversation last read it "
        f"({int(known['size'])} → {size} bytes, last seen {known['seen_at']}); "
        "fs_read it again and redo the change against what is there now"
    )


def written(app: Any, conversation_id: str) -> list[str]:
    """Paths this conversation wrote (for the permission classifier's script lookup)."""
    return [
        str(r["path"])
        for r in app.db.execute(
            "SELECT path FROM file_reads WHERE conversation_id=? AND wrote=1 ORDER BY seen_at DESC",
            (conversation_id,),
        )
    ]
