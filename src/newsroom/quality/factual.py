"""Factual QC — the render must cite a validated script (§47)."""
from __future__ import annotations

import sqlite3

from ..storage import repositories as repo


def check_facts(conn: sqlite3.Connection, manifest: dict) -> dict:
    result = {"ok": True, "issues": []}
    script = manifest.get("script", {})
    if not script.get("validated"):
        result["ok"] = False
        result["issues"].append("script not validated")
    story_id = manifest.get("story_id")
    if story_id:
        row = conn.execute(
            "SELECT validated FROM stories WHERE story_id=?", (story_id,)
        ).fetchone()
        if not row or not row["validated"]:
            result["ok"] = False
            result["issues"].append("no validated story row")
    if not manifest.get("fact_ids"):
        result["ok"] = False
        result["issues"].append("no fact ids in manifest")
    return result
