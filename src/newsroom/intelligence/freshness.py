"""Freshness tracking (§74).

Feeds that stop updating are marked stale rather than silently trusted —
during an outage the newsroom keeps last verified state and says so.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone


def latest_observation_age(conn: sqlite3.Connection, source: str) -> float | None:
    """Seconds since the source's newest stored observation. None = never."""
    row = conn.execute(
        "SELECT MAX(retrieved_at) FROM observations WHERE source=?", (source,)
    ).fetchone()
    if not row or not row[0]:
        return None
    ts = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - ts).total_seconds()


def feed_status(conn: sqlite3.Connection, source: str, stale_after_s: int = 600) -> dict:
    age = latest_observation_age(conn, source)
    return {
        "source": source,
        "age_seconds": age,
        "status": "unknown" if age is None else ("fresh" if age <= stale_after_s else "stale"),
    }
