"""Publication gate (§46, §47).

Every check must pass before a render could ever be published. Failure means
DO NOT PUBLISH — the manifest records the verdict either way.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..config import Settings
from ..editor.ffmpeg import ffprobe
from ..storage import repositories as repo
from .audio import check_render_audio
from .factual import check_facts
from .stale_revision import check_stale
from .visual import check_video


def gate_render(
    conn: sqlite3.Connection, settings: Settings, manifest: dict
) -> dict:
    checks: dict[str, dict] = {}
    checks["video"] = check_video(manifest)
    checks["audio"] = check_render_audio(manifest)
    checks["facts"] = check_facts(conn, manifest)
    checks["stale_revision"] = check_stale(conn, manifest)
    checks["provenance"] = _check_provenance(manifest)

    failures = {k: v for k, v in checks.items() if not v["ok"]}
    verdict = "pass" if not failures else "reject"
    # Publishing stays feature-gated even when all checks pass (§66).
    auto = bool(settings.get("publishing.automatic", False))
    return {
        "verdict": verdict,
        "automatic_publishing": auto,
        "would_publish": verdict == "pass" and auto,
        "checks": checks,
    }


def _check_provenance(manifest: dict) -> dict:
    required = ["event_id", "event_revision", "sources", "fact_ids",
                "script", "video", "platform"]
    missing = [k for k in required if not manifest.get(k)]
    ok = not missing and bool(manifest.get("fact_ids"))
    return {"ok": ok, "missing": missing}
