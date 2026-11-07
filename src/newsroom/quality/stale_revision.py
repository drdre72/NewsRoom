"""Stale-render protection (§46): render_revision must equal current revision."""
from __future__ import annotations

import sqlite3

from ..storage import repositories as repo


def check_stale(conn: sqlite3.Connection, manifest: dict) -> dict:
    event = repo.get_event(conn, manifest.get("event_id", ""))
    if not event:
        return {"ok": False, "issues": ["event not found"]}
    render_rev = manifest.get("event_revision")
    ok = render_rev == event.current_revision
    return {
        "ok": ok,
        "render_revision": render_rev,
        "current_revision": event.current_revision,
        "issues": [] if ok else [
            f"stale render: r{render_rev} < current r{event.current_revision}"],
    }
