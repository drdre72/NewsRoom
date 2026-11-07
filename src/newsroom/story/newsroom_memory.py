"""Newsroom memory (§17) — database state, not LLM memory.

Answers: what did we know before, what changed, what did we already air?
"""
from __future__ import annotations

import sqlite3

from ..intelligence.fact_ledger import FactLedger
from ..storage import repositories as repo


def what_changed(conn: sqlite3.Connection, event_id: str) -> list[dict]:
    """Field-level revision history derived from the ledger (§10)."""
    ledger = FactLedger(conn)
    out = []
    fields = {f.field for f in repo.all_facts(conn, event_id)}
    for field in sorted(fields):
        summary = ledger.revision_summary(event_id, field)
        if summary:
            out.append(summary)
    return out


def revision_timeline(conn: sqlite3.Connection, event_id: str) -> list[dict]:
    return [
        {"revision": r.revision, "at": r.created_at,
         "changed_fields": r.changed_fields}
        for r in repo.get_revisions(conn, event_id)
    ]


def was_aired(conn: sqlite3.Connection, event_id: str, revision: int) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM stories WHERE event_id=? AND revision=? AND validated=1",
        (event_id, revision),
    ).fetchone()
    return bool(row and row[0])
