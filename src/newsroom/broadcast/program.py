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
    "crime": ["crime_report"],
    "earthquakes": ["earthquake"],
    "world_weather": ["weather_alert"],
    "international": ["wildfire", "volcano", "severe_storm", "flood",
                      "landslide", "snow", "drought", "sea_ice",
                      "temp_extreme", "tropical_cyclone", "tsunami_warning",
                      "conflict_event", "protest", "news_report"],
    "space_weather": ["space_weather"],
    "us_desk": ["disaster_declaration", "cyber_advisory"],
    "markets": ["market_quote"],
    "economy": ["economic_indicator"],
    "explainer": ["economic_indicator"],
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
            # per-event air cadence: don't repeat a story inside the window.
            # Replay airs are filler — they never count toward the cadence.
            last = conn.execute(
                "SELECT MAX(started_at) FROM air_log WHERE event_id=?"
                " AND kind != 'replay'"
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


_LEVEL_RANK = {"FLASH": 4, "BREAKING": 3, "UPDATE": 2,
               "NORMAL": 1, "BACKGROUND": 0}


def pick_next(conn: sqlite3.Connection, settings: Settings) -> Program | None:
    """Rundown, severity-first: each segment nominates its best eligible
    event; the most important candidate across ALL segments airs next
    (level -> score -> recency). Segment priority only breaks ties —
    it decides which desk a story airs under, not whether it airs."""
    segments = settings.get("rundown.segments", []) or []
    candidates = []
    for seg in segments:
        seg_id = seg.get("id")
        cadence_s = int(seg.get("max_air_every_min", 30)) * 60
        if repo.aired_recently(conn, seg_id, cadence_s):
            continue  # segment aired inside its cadence window
        eligible = _eligible_events(conn, settings,
                                    SEGMENT_TYPES.get(seg_id), cadence_s)
        if eligible:
            candidates.append((seg, eligible[0]))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (
        -_LEVEL_RANK.get(c[1].impact_level, 0),
        -c[1].impact_score,
        str(c[1].last_updated or ""),
        -int(c[0].get("priority", 0))))
    seg, ev = candidates[0]
    asset = repo.latest_video_asset(conn, ev.event_id, ev.current_revision)
    return Program("story", seg["id"], ev.event_id, ev.current_revision,
                   asset["path"], ev.title or ev.event_id, ev.impact_level)


def pick_replay(conn: sqlite3.Connection, settings: Settings) -> Program | None:
    """Filler rotation for an empty rundown (§31): re-air stories whose
    *current* revision already premiered, least-recently-aired first so the
    rotation is a fair round-robin. The current-revision rule keeps replays
    from stealing a premiere — a fresh revision that lands while its segment
    is in cadence stays the fresh path's job, and the event only re-enters
    this pool once that revision has properly aired."""
    if settings.get("replay.enabled", True) is False:
        return None
    min_conf = float(settings.get("auto_render.min_confidence", 0.70))
    rows = conn.execute(
        "SELECT a.event_id, MAX(a.started_at) AS last_air,"
        " (julianday('now') - julianday(MAX(a.started_at))) * 24.0 AS stale_h"
        " FROM air_log a"
        " JOIN events e ON e.event_id = a.event_id"
        " AND e.current_revision = a.revision"
        " GROUP BY a.event_id ORDER BY last_air ASC",
    ).fetchall()
    # Severity-weighted LRU: a story's impact multiplies its staleness, so
    # BREAKING crime rotates back roughly twice as fast as NORMAL filler
    # while the pool still round-robins within a level.
    candidates = []
    for row in rows:
        ev = repo.get_event(conn, row["event_id"])
        if not ev or ev.confidence_score < min_conf:
            continue
        asset = repo.latest_video_asset(conn, ev.event_id,
                                        ev.current_revision)
        if not asset:
            continue
        weight = 1.0 + 0.75 * _LEVEL_RANK.get(ev.impact_level, 0)
        candidates.append((-(row["stale_h"] or 0.0) * weight, ev, asset))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _, ev, asset = candidates[0]
    return Program("replay", "replay", ev.event_id, ev.current_revision,
                   asset["path"], ev.title or ev.event_id, ev.impact_level)
