"""GDACS adapter — UN/EU Global Disaster Alert and Coordination System RSS.

Global disaster alerts with Red/Orange/Green alert levels and population
exposure. Earthquake events are INGESTED (not skipped): they fuse with USGS
canonical events via time+distance correlation, earning the corroboration
bonus — and GDACS-only quakes still get coverage (§16).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Iterable

from .base import SourceAdapter
from ...models.observation import NormalizedObservation
from .nws import _iso

_GDACS_NS = "{http://www.gdacs.org}"
_GEO_NS = "{http://www.w3.org/2003/01/geo/wgs84_pos#}"

_TYPE_MAP = {
    "EQ": "earthquake",
    "TC": "tropical_cyclone",
    "FL": "flood",
    "VO": "volcano",
    "DR": "drought",
    "WF": "wildfire",
    "TS": "tsunami_warning",
}

_TYPE_TITLE = {
    "EQ": "Earthquake", "TC": "Tropical Cyclone", "FL": "Flood",
    "VO": "Volcano", "DR": "Drought", "WF": "Wildfire", "TS": "Tsunami",
}


def _text(item: ET.Element, tag: str) -> str | None:
    el = item.find(tag)
    return el.text.strip() if el is not None and el.text else None


class GdacsAdapter(SourceAdapter):
    kind = "gdacs"
    event_type = "gdacs_event"      # per-item type set in normalize()

    def split_items(self, body: str) -> Iterable[str]:
        root = ET.fromstring(body)
        for item in root.iter("item"):
            yield ET.tostring(item, encoding="unicode")

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        try:
            item = ET.fromstring(raw_payload)
        except ET.ParseError:
            return None
        gtype = _text(item, f"{_GDACS_NS}eventtype")
        event_type = _TYPE_MAP.get(gtype or "")
        event_id = _text(item, f"{_GDACS_NS}eventid") or _text(item, "guid")
        if not event_type or not event_id:
            return None
        ep_id = _text(item, f"{_GDACS_NS}episodeid")
        if ep_id:
            event_id = f"{event_id}.{ep_id}"
        alert = _text(item, f"{_GDACS_NS}alertlevel")
        sev_el = item.find(f"{_GDACS_NS}severity")
        sev = None
        sev_unit = None
        if sev_el is not None:
            try:
                sev = float(sev_el.get("value"))
            except (TypeError, ValueError):
                pass
            sev_unit = sev_el.get("unit") or None
        lat = _text(item, f"{_GEO_NS}Point/{_GEO_NS}lat")
        lon = _text(item, f"{_GEO_NS}Point/{_GEO_NS}long")
        title = _text(item, "title")
        country = _text(item, f"{_GDACS_NS}country")
        fields = {
            "alert_level": alert,
            "gdacs_type": gtype,
            "category": gtype,
            "category_title": _TYPE_TITLE.get(gtype or ""),
            "country": country,
            "population": _text(item, f"{_GDACS_NS}population"),
            "magnitude_unit": sev_unit,
            "source_url": _text(item, "link"),
            "description": _text(item, "description"),
        }
        return NormalizedObservation(
            source="gdacs",
            source_event_id=event_id,
            event_type=event_type,
            occurred_at=_iso(_text(item, f"{_GDACS_NS}fromdate")
                             or _text(item, "pubDate")) or "",
            source_updated_at=_iso(_text(item, "pubDate")),
            latitude=_f(lat), longitude=_f(lon),
            location_label=country or None,
            title=title,
            severity=sev,
            status="active",
            reviewed=False,
            fields=fields,
            cross_references=[],
        )


def _f(v: str | None) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
