"""SQLite ownership layer (§75).

Single-writer discipline: the newsroom core owns writes; connections are
short-lived, WAL-mode, with busy timeouts so a crashed render worker can
never wedge the database.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..paths import db_path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
SCHEMA_VERSION = 1


def connect(path: Path | None = None) -> sqlite3.Connection:
    # isolation_level=None -> autocommit: each statement persists immediately
    # unless wrapped in tx(). An observation's writes (evidence row, event
    # sources, revision, facts, event upsert) commit atomically via tx().
    conn = sqlite3.connect(path or db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """Transaction boundary — BEGIN/COMMIT, rollback on error. Reentrant:
    a tx() nested inside another tx() defers to the outer boundary."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns current schema version."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {
        row[0]
        for row in conn.execute("SELECT version FROM schema_migrations")
    }
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(sql_file.name.split("_")[0])
        if version in applied:
            continue
        # executescript() runs in autocommit (it commits pending txns itself),
        # so it stays outside tx(); only the version marker needs a tx.
        conn.executescript(sql_file.read_text(encoding="utf-8"))
        with tx(conn):
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (version,),
            )
    return max(applied | {SCHEMA_VERSION})


def integrity_check(conn: sqlite3.Connection) -> dict:
    """Startup validation (§75): integrity + migration state + counts."""
    result: dict = {}
    row = conn.execute("PRAGMA integrity_check").fetchone()
    result["integrity"] = row[0] if row else "unknown"
    result["wal"] = conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    try:
        result["schema_version"] = conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        result["schema_version"] = 0
    for table in ("observations", "events", "facts", "jobs", "stories", "assets"):
        try:
            result[f"count_{table}"] = conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            result[f"count_{table}"] = None
    return result


def recover_jobs(conn: sqlite3.Connection) -> int:
    """Requeue jobs left 'running' by an unclean shutdown (§39, §75)."""
    with tx(conn):
        cur = conn.execute(
            "UPDATE jobs SET status='queued', started_at=NULL, "
            "error='recovered after restart' WHERE status='running'"
        )
    return cur.rowcount
