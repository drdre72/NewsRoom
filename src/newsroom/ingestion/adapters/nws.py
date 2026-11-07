"""NWS CAP alert adapter (api.weather.gov).

The /alerts/active feed is a GeoJSON FeatureCollection where each feature's
properties carry CAP fields (event, severity, certainty, urgency, onset,
expires, instruction...). NWS updates alerts in place: the same alert id
reappears with revised properties, which drives our revision history.

Geometry is a polygon (or null for zone-based alerts); we reduce it to a
centroid for mapping and fusion, and a bbox for the snapshot.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from .base import SourceAdapter
from ...models.observation import NormalizedObservation

# CAP severity -> numeric scale for ranking/scoring (like Saffir-Simpson).
# The verbatim text is kept in `severity_text` for citation.
_SEVERITY_SCALE = {
    "minor": 1.0, "moderate": 2.0, "severe": 3.0, "extreme": 4.0,
}

# CAP status -> canonical event status.
_STATUS_MAP = {"actual": "active", "test": "test", "draft": "draft",
               "exercise": "exercise", "system": "system"}


def _iso(value: Any) -> str | None:
    """NWS timestamps arrive as ISO-8601 with offsets or 'Z'. Normalize to Z."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _centroid(geometry: dict | None) -> tuple[float | None, float | None]:
    """Mean of the exterior ring vertices — good enough for fusion/mapping."""
    if not geometry:
        return None, None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    ring: list = []
    if gtype == "Polygon" and coords:
        ring = coords[0]
    elif gtype == "MultiPolygon" and coords and coords[0]:
        ring = coords[0][0]
    elif gtype == "Point" and len(coords) >= 2:
        return coords[1], coords[0]
    if not ring:
        return None, None
    lats = [p[1] for p in ring if isinstance(p, (list, tuple)) and len(p) >= 2]
    lons = [p[0] for p in ring if isinstance(p, (list, tuple)) and len(p) >= 2]
    if not lats or not lons:
        return None, None
    return (round(sum(lats) / len(lats), 4), round(sum(lons) / len(lons), 4))


def _bbox(geometry: dict | None) -> list[float] | None:
    if not geometry:
        return None
    flat: list = []

    def walk(c):
        if isinstance(c, (list, tuple)):
            if len(c) >= 2 and all(isinstance(v, (int, float)) for v in c[:2]):
                flat.append(c)
            else:
                for x in c:
                    walk(x)
    walk(geometry.get("coordinates"))
    if not flat:
        return None
    lons = [p[0] for p in flat]
    lats = [p[1] for p in flat]
    return [round(min(lons), 4), round(min(lats), 4),
            round(max(lons), 4), round(max(lats), 4)]


class NwsAdapter(SourceAdapter):
    kind = "nws_cap"
    event_type = "weather_alert"

    def split_items(self, body: str) -> Iterable[str]:
        doc = json.loads(body)
        features = doc.get("features")
        if features is None:                      # bare Feature
            yield json.dumps(doc, separators=(",", ":"), sort_keys=True)
            return
        for feature in features:
            yield json.dumps(feature, separators=(",", ":"), sort_keys=True)

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        f = json.loads(raw_payload)
        if f.get("type") == "FeatureCollection":   # tolerate unsplit feeds
            f = (f.get("features") or [{}])[0]
        props = f.get("properties") or {}
        # Only Actual alerts are real-world events. Test/Draft/Exercise/System
        # statuses include NWS keep-alive heartbeats — protocol noise, though
        # the raw payload is still stored as evidence upstream (§9).
        if str(props.get("status") or "").lower() != "actual":
            return None
        alert_id = f.get("id") or props.get("@id") or props.get("id")
        if not alert_id:
            return None
        # api.weather.gov uses a URL as the feature id; the URN is the tail.
        alert_id = str(alert_id).rsplit("/", 1)[-1]
        params = props.get("parameters") or {}
        motion = (params.get("eventMotionDescription") or [None])[0]
        if motion:
            motion = motion.strip(". ").strip() or None
        lat, lon = _centroid(f.get("geometry"))
        event_name = props.get("event") or "Weather Alert"
        area = props.get("areaDesc")
        status = (props.get("status") or "").lower()
        msg_type = props.get("messageType") or ""
        references = []
        for ref in props.get("references") or []:
            if isinstance(ref, dict):
                rid = ref.get("identifier") or ref.get("@id") or ref.get("id")
            else:
                rid = ref
            if rid:
                references.append(str(rid))
        fields = {
            "alert_event": event_name,
            "severity_text": props.get("severity"),
            "certainty": props.get("certainty"),
            "urgency": props.get("urgency"),
            "response": props.get("response"),
            "sender_name": props.get("senderName"),
            "headline": props.get("headline"),
            "description": props.get("description"),
            "instruction": props.get("instruction"),
            "effective": _iso(props.get("effective")),
            "onset": _iso(props.get("onset")),
            "expires": _iso(props.get("expires")),
            "ends": _iso(props.get("ends")),
            "message_type": msg_type or None,
            "awips_id": (params.get("AWIPSidentifier") or [None])[0],
            "wmo_id": (params.get("WMOidentifier") or [None])[0],
            "motion": motion,
            "polygon_bbox": _bbox(f.get("geometry")),
        }
        return NormalizedObservation(
            source="nws",
            source_event_id=str(alert_id),
            event_type="weather_alert",
            occurred_at=_iso(props.get("onset")) or _iso(props.get("effective"))
                or _iso(props.get("sent")) or "",
            source_updated_at=_iso(props.get("sent")),
            latitude=lat,
            longitude=lon,
            location_label=area,
            title=f"{event_name} — {area}" if area else event_name,
            severity=_SEVERITY_SCALE.get(
                (props.get("severity") or "").lower(), 0.0),
            status=_STATUS_MAP.get(status, status or None),
            reviewed=(status == "actual" and msg_type in ("Alert", "Update")),
            fields=fields,
            cross_references=references,
        )
