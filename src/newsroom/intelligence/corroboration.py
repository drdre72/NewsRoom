"""Corroboration helpers (§14, §16)."""
from __future__ import annotations

import sqlite3


def corroborating_source_count(conn: sqlite3.Connection, event_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT source) FROM event_sources WHERE event_id=?",
        (event_id,),
    ).fetchone()
    return row[0] if row else 0
