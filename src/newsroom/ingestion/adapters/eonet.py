"""NASA EONET adapter (eonet.gsfc.nasa.gov/api/v3/events).

Curated natural events: wildfires, volcanoes, severe storms, floods,
landslides, drought, snow, sea/lake ice, temperature extremes. Each event
carries a category and a geometry track (points, sometimes polygons) with
dates — the latest geometry entry is the event's current position.

Earthquakes, water color, dust/haze and man-made categories are skipped:
USGS/NWS are our authorities for those domains (§16), and the rest carry
little broadcast value. Raw payloads are stored as evidence regardless.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from .base import SourceAdapter
from ...models.observation import NormalizedObservation
from .nws import _centroid, _iso

# EONET category id -> newsroom event_type.
_CATEGORY_TYPE = {
    "wildfires": "wildfire",
    "severeStorms": "severe_storm",
    "volcanoes": "volcano",
    "floods": "flood",
    "landslides": "landslide",
    "snow": "snow",
    "drought": "drought",
    "seaLakeIce": "sea_ice",
    "tempExtremes": "temp_extreme",
}

# Categories we deliberately do not report: another source owns the domain
# (earthquakes -> USGS) or the content is not newsworthy for this channel.
_SKIP_CATEGORIES = {"earthquakes", "waterColor", "dustHaze", "manmade"}


def _latest_geometry(geometry: list[dict] | None) -> dict | None:
    """Newest geometry entry by date — the event's current position."""
    if not geometry:
        return None
    return max(geometry, key=lambda g: str(g.get("date") or ""))


def _label_from_title(title: str) -> str | None:
    """'Wildfire - Riverside County, California' -> 'Riverside County, ...'"""
    if " - " in title:
        tail = title.split(" - ", 1)[1].strip()
        return tail or None
    return None


class EonetAdapter(SourceAdapter):
    kind = "nasa_eonet"
    event_type = "eonet_event"      # per-item type set in normalize()

    def split_items(self, body: str) -> Iterable[str]:
        doc = json.loads(body)
        events = doc.get("events")
        if events is None:                      # tolerate a bare event object
            yield json.dumps(doc, separators=(",", ":"), sort_keys=True)
            return
        for event in events:
            yield json.dumps(event, separators=(",", ":"), sort_keys=True)

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        ev = json.loads(raw_payload)
        event_id = ev.get("id")
        if not event_id:
            return None
        cats = ev.get("categories") or []
        cat_ids = [str(c.get("id") or "") for c in cats if isinstance(c, dict)]
        if cat_ids and all(c in _SKIP_CATEGORIES for c in cat_ids):
            return None
        event_type = next(
            (_CATEGORY_TYPE[c] for c in cat_ids if c in _CATEGORY_TYPE), None)
        if not event_type:
            return None                     # unmapped category — no opinion
        primary_cat = next(c for c in cat_ids if c in _CATEGORY_TYPE)
        cat_title = next(
            (c.get("title") for c in cats
             if isinstance(c, dict) and c.get("id") == primary_cat), None)
        geom = _latest_geometry(ev.get("geometry"))
        lat, lon = (None, None)
        if geom:
            lat, lon = _centroid({"type": geom.get("type"),
                                  "coordinates": geom.get("coordinates")})
        occurred = _iso(geom.get("date")) if geom else None
        closed = ev.get("closed")
        sources = [s.get("url") for s in (ev.get("sources") or [])
                   if isinstance(s, dict) and s.get("url")]
        title = ev.get("title") or cat_title or event_id
        fields = {
            "category": primary_cat,
            "category_title": cat_title,
            "closed": bool(closed),
            "closed_at": _iso(closed),
            "magnitude_unit": (geom or {}).get("magnitudeUnit"),
            "source_url": sources[0] if sources else None,
            "eonet_link": ev.get("link"),
            "description": ev.get("description"),
        }
        return NormalizedObservation(
            source="eonet",
            source_event_id=str(event_id),
            event_type=event_type,
            occurred_at=occurred or "",
            source_updated_at=occurred,
            latitude=lat, longitude=lon,
            location_label=_label_from_title(title),
            title=title,
            severity=(geom or {}).get("magnitudeValue"),
            status="closed" if closed else "active",
            reviewed=False,
            fields=fields,
            cross_references=sources[1:],
        )
