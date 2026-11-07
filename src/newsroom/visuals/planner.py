"""Visual planner (§19) — deterministic shot intent from fact state.

The plan is data; renderers execute it verbatim. Shot durations are sized
so the visual plan covers the narration timeline.
"""
from __future__ import annotations

import sqlite3

from ..models.event import CanonicalEvent
from ..models.fact import Fact
from ..models.shot import Shot, VisualPlan
from ..models.story import Script
from ..storage import repositories as repo


def _fmap(facts: list[Fact]) -> dict[str, Fact]:
    return {f.field: f for f in facts}


# Metric-grid fields per event family (first six render).
_METRICS_BY_TYPE = {
    "earthquake": (("magnitude", "MAGNITUDE"), ("depth_km", "DEPTH"),
                   ("occurred_at", "TIME UTC"), ("tsunami", "TSUNAMI"),
                   ("alert", "PAGER ALERT"), ("sig", "SIGNIFICANCE"),
                   ("mmi", "MAX MMI"), ("status", "STATUS")),
    "weather_alert": (("alert_event", "ALERT"), ("severity_text", "SEVERITY"),
                      ("certainty", "CERTAINTY"), ("urgency", "URGENCY"),
                      ("expires", "EXPIRES"), ("response", "RESPONSE"),
                      ("sender_name", "OFFICE")),
}
_METRICS_GENERIC = (("title", "EVENT"), ("occurred_at", "TIME UTC"),
                    ("status", "STATUS"))

# EONET/GDACS/SWPC families share one metric grid — extent + unit, class,
# time, status.
_EONET_TYPES = {"wildfire", "volcano", "severe_storm", "flood", "landslide",
                "snow", "drought", "sea_ice", "temp_extreme",
                "tropical_cyclone", "tsunami_warning", "space_weather",
                "conflict_event", "protest"}
_METRICS_EONET = (("severity", "EXTENT"), ("magnitude_unit", "UNIT"),
                  ("category_title", "CLASS"), ("occurred_at", "TIME UTC"),
                  ("status", "STATUS"))


def _metrics_for(event_type: str):
    if event_type in _EONET_TYPES:
        return _METRICS_EONET
    return _METRICS_BY_TYPE.get(event_type, _METRICS_GENERIC)


def plan_visuals(
    conn: sqlite3.Connection,
    event: CanonicalEvent,
    script: Script,
    width: int,
    height: int,
    fps: int,
) -> VisualPlan:
    f = _fmap(repo.current_facts(conn, event.event_id))
    total_speech = sum(s.duration_s for s in script.segments) or 30.0

    shots: list[Shot] = [
        Shot(type="title_card", duration_s=3.5, params={
            "headline": script.headline, "level": event.impact_level,
            "location": event.location_label,
        }),
    ]

    if event.latitude is not None and event.longitude is not None:
        shots.append(Shot(type="locator_map", duration_s=min(8.0, total_speech * 0.35),
                          params={
                              "lat": event.latitude, "lon": event.longitude,
                              "label": event.location_label,
                              "zoom": 4.5,
                              "rings_km": [100, 250, 500],
                              "depth_km": f.get("depth_km").value_display if f.get("depth_km") else None,
                              "magnitude": f["magnitude"].value_display if "magnitude" in f else None,
                              "tsunami": bool(f["tsunami"].value) if "tsunami" in f else None,
                          }))

    metrics = []
    for field, label in _metrics_for(event.event_type):
        if field in f:
            metrics.append({"label": label, "value": f[field].value_display})
    if metrics:
        shots.append(Shot(type="metric_grid", duration_s=min(10.0, total_speech * 0.4),
                          params={"metrics": metrics, "title": "Event Data"}))

    changed = [s for s in repo.get_revisions(conn, event.event_id)]
    if len(changed) > 1:
        rev_fields = []
        for r in changed[1:]:
            rev_fields.extend(x for x in r.changed_fields if x not in rev_fields)
        shots.append(Shot(type="revision_card", duration_s=6.0, params={
            "revision": event.current_revision,
            "changed": rev_fields[:6],
        }))

    shots.append(Shot(type="source_card", duration_s=4.0, params={
        "sources": event.source_ids, "updated": event.last_updated,
        "confidence": event.confidence_score,
        "preliminary": (event.extra.get("source_status") != "reviewed"),
    }))

    return VisualPlan(event_id=event.event_id, revision=event.current_revision,
                      width=width, height=height, fps=fps, shots=shots)
