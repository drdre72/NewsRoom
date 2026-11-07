"""USGS GeoJSON earthquake feed adapter.

Feeds (see sources.yaml): summary GeoJSON documents where each feature is a
self-contained event snapshot. USGS revises events in place — the same
feature id reappears with updated properties, which is what drives our
revision history.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from .base import FetchResult, SourceAdapter
from ...models.observation import NormalizedObservation


def _ms_to_iso(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class UsgsAdapter(SourceAdapter):
    kind = "usgs_geojson"
    event_type = "earthquake"

    def split_items(self, body: str) -> Iterable[str]:
        doc = json.loads(body)
        for feature in doc.get("features", []):
            yield json.dumps(feature, separators=(",", ":"), sort_keys=True)

    def feed_metadata(self, body: str) -> dict[str, Any]:
        doc = json.loads(body)
        return doc.get("metadata", {}) or {}

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        f = json.loads(raw_payload)
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or [None, None, None]
        source_event_id = f.get("id") or props.get("code")
        if not source_event_id:
            return None
        mag = props.get("mag")
        lon, lat, depth_km = (coords + [None, None, None])[:3]
        fields = {
            "magnitude": mag,
            "mag_type": props.get("magType"),
            "depth_km": depth_km,
            "tsunami": bool(props.get("tsunami")),
            "alert": props.get("alert"),           # PAGER: green/yellow/orange/red
            "sig": props.get("sig"),
            "mmi": props.get("mmi"),
            "felt": props.get("felt"),
            "net": props.get("net"),
            "code": props.get("code"),
            "ids": props.get("ids"),
            "sources": props.get("sources"),
            "types": props.get("types"),
            "url": props.get("url"),
        }
        return NormalizedObservation(
            source="usgs",
            source_event_id=str(source_event_id),
            event_type="earthquake",
            occurred_at=_ms_to_iso(props.get("time")) or "",
            source_updated_at=_ms_to_iso(props.get("updated")),
            latitude=lat,
            longitude=lon,
            location_label=props.get("place"),
            title=props.get("title"),
            severity=mag,
            status=props.get("status"),
            reviewed=(props.get("status") == "reviewed"),
            fields=fields,
            cross_references=[
                i for i in (props.get("ids") or "").split(",") if i and i != source_event_id
            ],
        )
