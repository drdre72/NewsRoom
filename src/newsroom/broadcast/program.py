"""Rundown scheduling — what airs next (§31-§33).

Deterministic selection: a rendered, gate-passed video asset at the event's
current revision, meeting confidence, not aired within the segment's
max_air_every_min cadence. Interrupts (FLASH/BREAKING) preempt the rundown
at program boundaries — and mid-program for FLASH.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..config import Settings
from ..storage import repositories as repo

# Rundown segment id -> event_types covered. Segments without a mapping
# have no content source yet and are skipped (headlines has no list =
# any event type qualifies).
SEGMENT_TYPES = {
    "earthquakes": ["earthquake"],
    "world_weather": ["weather_alert"],
    "international": ["wildfire", "volcano", "severe_storm", "flood",
                      "landslide", "snow", "drought", "sea_ice",
                      "temp_extreme"],
}


@dataclass
class Program:
    kind: str               # 'story' | 'standby' | 'interrupt'
    rundown_id: str | None
    event_id: str | None
    revision: int | None
    asset_path: str | None
    title: str
    level: str | None


def _eligible_events(conn: sqlite3.Connection, settings: Settings,
                     event_types: list[str] | None, max_air_s: int) -> list:
    """Events with a rendered video asset at current revision, meeting
    confidence, not aired anywhere within the cadence window."""
    min_conf = float(settings.get("auto_render.min_confidence", 0.70))
    types = event_types or [None]
    out = []
    seen = set()
    for et in types:
        for ev in repo.list_events(conn, event_type=et, limit=50):
            if ev.event_id in seen:
                continue
            seen.add(ev.event_id)
            if ev.confidence_score < min_conf:
                continue
            if not repo.latest_video_asset(
                    conn, ev.event_id, ev.current_revision):
                continue
            # per-event air cadence: don't repeat a story inside the window
            last = conn.execute(
                "SELECT MAX(started_at) FROM air_log WHERE event_id=?"
                " AND started_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now', ?)",
                (ev.event_id, f"-{max_air_s} seconds"),
            ).fetchone()[0]
            if last:
                continue
            out.append(ev)
    # impact_score first, then recency (list is already recency-ordered)
    out.sort(key=lambda e: e.impact_score, reverse=True)
    return out


def pick_interrupt(conn: sqlite3.Connection, settings: Settings) -> Program | None:
    """An unaired FLASH/BREAKING revision eligible to preempt programming."""
    interrupt = settings.get("interrupt", {}) or {}
    flash_min = float(interrupt.get("flash_min_confidence", 0.95))
    break_min = float(interrupt.get("breaking_min_confidence", 0.90))
    for ev in repo.list_events(conn, limit=50):
        need = flash_min if ev.impact_level == "FLASH" else \
            break_min if ev.impact_level == "BREAKING" else None
        if need is None or ev.confidence_score < need:
            continue
        asset = repo.latest_video_asset(conn, ev.event_id, ev.current_revision)
        if not asset:
            continue
        # only interrupt for revisions we have not already aired
        aired = conn.execute(
            "SELECT 1 FROM air_log WHERE event_id=? AND revision=? LIMIT 1",
            (ev.event_id, ev.current_revision),
        ).fetchone()
        if aired:
            continue
        return Program("interrupt", None, ev.event_id, ev.current_revision,
                       asset["path"], ev.title or ev.event_id, ev.impact_level)
    return None


def pick_next(conn: sqlite3.Connection, settings: Settings) -> Program | None:
    """Rundown order: segments by priority; first eligible item wins."""
    segments = settings.get("rundown.segments", []) or []
    segments = sorted(segments, key=lambda s: -int(s.get("priority", 0)))
    for seg in segments:
        seg_id = seg.get("id")
        cadence_s = int(seg.get("max_air_every_min", 30)) * 60
        if repo.aired_recently(conn, seg_id, cadence_s):
            continue  # segment aired inside its cadence window
        for ev in _eligible_events(conn, settings,
                                   SEGMENT_TYPES.get(seg_id), cadence_s):
            asset = repo.latest_video_asset(
                conn, ev.event_id, ev.current_revision)
            return Program("story", seg_id, ev.event_id, ev.current_revision,
                           asset["path"], ev.title or ev.event_id,
                           ev.impact_level)
    return None
