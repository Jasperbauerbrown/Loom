"""Logging: one rotating file, stderr, and an in-memory ring the System screen reads.

`setup(home, level)` is idempotent — calling it twice reconfigures rather than stacking
handlers, so tests and `serve` can both call it. `recent(...)` backs
`GET /api/system/logs`; `set_level(...)` backs a live `system.log_level` change, which
`listen(app)` wires to the bus so the setting and the running process never disagree.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from collections import deque
from pathlib import Path
from typing import Any, Deque

RING_SIZE = 2000
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class RingHandler(logging.Handler):
    """Keeps the last `size` records as plain dicts, newest last."""

    def __init__(self, size: int = RING_SIZE):
        super().__init__()
        self.records: Deque[dict[str, Any]] = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
        except Exception:  # pragma: no cover - formatting must never kill logging
            message = str(record.msg)
        self.records.append(
            {
                "ts": _iso(record.created),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            }
        )


def _iso(created: float) -> str:
    import datetime as _dt

    return (
        _dt.datetime.fromtimestamp(created, _dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


_ring: RingHandler | None = None
_handlers: list[logging.Handler] = []


def setup(home: Path, level: str = "INFO") -> RingHandler:
    """Install the file + stderr + ring handlers on the root logger. Returns the ring."""
    global _ring
    root = logging.getLogger()
    for handler in _handlers:
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    _handlers.clear()

    logs_dir = Path(home) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(FORMAT)

    new: list[logging.Handler] = []
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            logs_dir / "backend.log", maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        new.append(file_handler)
    except OSError:
        # A log file we cannot open is not a reason to refuse to run.
        pass
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(formatter)
    new.append(stderr)

    _ring = RingHandler()
    new.append(_ring)

    for handler in new:
        root.addHandler(handler)
    _handlers.extend(new)
    set_level(level)
    return _ring


def set_level(level: str) -> None:
    resolved = level.upper() if isinstance(level, str) else "INFO"
    if resolved not in LEVELS:
        resolved = "INFO"
    logging.getLogger().setLevel(getattr(logging, resolved))


def ring() -> RingHandler | None:
    return _ring


def recent(since_iso: str | None = None, level: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    """Newest-last slice of the ring, filtered by timestamp and minimum level."""
    if _ring is None:
        return []
    floor = LEVELS.index(level.upper()) if level and level.upper() in LEVELS else 0
    out = []
    for rec in _ring.records:
        if since_iso and rec["ts"] <= since_iso:
            continue
        name = rec["level"]
        rank = LEVELS.index(name) if name in LEVELS else len(LEVELS) - 1
        if rank < floor:
            continue
        out.append(rec)
    if limit and limit > 0:
        out = out[-limit:]
    return out


def listen(app) -> None:  # type: ignore[no-untyped-def]
    """Follow `system.log_level` changes live (charter §2.2: no setting without effect)."""

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") != "settings.changed":
            return
        keys = (event.get("data") or {}).get("keys") or []
        if "system.log_level" in keys:
            set_level(app.settings.get("system.log_level"))

    app.bus.listen(on_event)
