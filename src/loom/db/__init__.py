"""SQLite connection and migrations. Stdlib sqlite3, WAL, foreign keys on.

Decision (2026-09-20): one synchronous connection per process, used from the event-loop
thread only. Local SQLite calls are sub-millisecond; an async wrapper would add a
dependency and a codepath for nothing. Blocking work that is *not* SQLite (subprocesses,
git, HTTP) goes through asyncio primitives or `asyncio.to_thread`.

Conventions:
- `isolation_level=None` (autocommit). Multi-statement units wrap themselves in
  `with transaction(conn):`.
- Timestamps are ISO-8601 UTC strings from `now_iso()`.
- JSON columns are written with `json.dumps(..., sort_keys=True)`.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    return json.loads(text)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the state database with the project's pragmas."""
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on exception. Re-entrant calls are not
    supported; keep units small."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def list_migrations() -> list[tuple[int, Path]]:
    out = []
    for p in sorted(MIGRATIONS_DIR.iterdir()):
        m = _MIGRATION_RE.match(p.name)
        if m:
            out.append((int(m.group(1)), p))
    return out


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply every migration above `PRAGMA user_version`, each in its own transaction.
    Returns the versions applied."""
    applied: list[int] = []
    version = current_version(conn)
    for number, path in list_migrations():
        if number <= version:
            continue
        sql = path.read_text(encoding="utf-8")
        with transaction(conn):
            for statement in split_statements(sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={number}")
        applied.append(number)
        version = number
    return applied


def split_statements(sql: str) -> list[str]:
    """Split a migration file into statements. Handles `BEGIN ... END;` trigger bodies
    and `--` comments; does not handle string literals containing ';' (avoid them)."""
    statements: list[str] = []
    buf: list[str] = []
    depth = 0
    for raw in sql.splitlines():
        line = raw.split("--", 1)[0].rstrip()
        if not line.strip():
            continue
        upper = line.strip().upper()
        if upper.startswith("BEGIN"):
            depth += 1
        buf.append(line)
        if upper.startswith("END") and depth > 0:
            depth -= 1
        if line.rstrip().endswith(";") and depth == 0:
            statements.append("\n".join(buf))
            buf = []
    if buf:
        statements.append("\n".join(buf))
    return statements


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else {k: row[k] for k in row.keys()}
