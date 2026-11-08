"""EIA adapter — U.S. Energy Information Administration open data (eia.gov).

Each configured series (weekly petroleum stocks, gas storage) is a stable
`economic_indicator` event keyed by its short series code; each new release
revises it. severity_norm is the surprise: |pct change vs previous print| /
per-series move_scale. EIA_API_KEY env only — v2 /seriesid/{id} endpoint.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Iterable

from .base import SourceAdapter, FetchResult
from ...models.observation import NormalizedObservation
from ...security import fetch_bytes

_BASE = "https://api.eia.gov/v2/seriesid/"


class EiaAdapter(SourceAdapter):
    kind = "eia"
    event_type = "economic_indicator"

    def fetch(self, timeout: int = 20, user_agent: str = "newsroom/0.1") -> FetchResult:
        from ...storage.repositories import utcnow
        key = os.environ.get("EIA_API_KEY", "")
        if not key:
            raise RuntimeError("EIA_API_KEY not set")
        payloads = []
        for sid in self.config.get("series", {}):
            q = urllib.parse.urlencode(
                {"api_key": key, "length": 2})
            url = f"{_BASE}{urllib.parse.quote(sid)}?{q}"
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            doc = json.loads(fetch_bytes(req, timeout=timeout).decode("utf-8"))
            if doc.get("error"):
                raise RuntimeError(f"EIA {sid}: {doc['error']}")
            resp_doc = doc.get("response") or {}
            payloads.append(json.dumps(
                {"series_id": sid,
                 "frequency": resp_doc.get("frequency"),
                 "rows": resp_doc.get("data", [])}))
        return FetchResult(feed_url=_BASE + "?[key]",
                           retrieved_at=utcnow(), payloads=payloads)

    def split_items(self, body: str) -> Iterable[str]:
        yield body                       # one payload per series already

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        doc = json.loads(raw_payload)
        sid = doc.get("series_id")
        rows = [r for r in (doc.get("rows") or [])
                if r.get("value") not in (None, "", ".")]
        if not rows or not sid:
            return None
        latest, prev = rows[0], (rows[1] if len(rows) > 1 else None)
        try:
            value = float(latest["value"])
        except (KeyError, TypeError, ValueError):
            return None
        # The API response carries both the request path id and a short
        # `series` code — the short code makes the cleaner event key.
        code = str(latest.get("series") or sid)
        meta = (self.config.get("series") or {}).get(sid, {}) or {}
        label = meta.get("label") or latest.get("series-description") or sid
        units = meta.get("units") or latest.get("units") or ""
        scale = float(meta.get("move_scale", 3.0))
        delta = delta_pct = None
        if prev:
            try:
                pv = float(prev["value"])
                delta = value - pv
                delta_pct = round((delta / pv * 100.0) if pv else 0.0, 2)
            except (TypeError, ValueError):
                pass
        snorm = min(1.0, abs(delta_pct or 0.0) / scale)
        fields = {
            "series_id": code, "series_path": sid, "series_label": label,
            "units": units, "value_date": latest.get("period"),
            "previous_value": prev.get("value") if prev else None,
            "delta": delta, "delta_pct": delta_pct,
            "period_name": latest.get("period"),
            "category": label, "category_title": "Energy Inventories",
            "severity_norm": snorm, "magnitude_unit": units,
            "frequency": (doc.get("frequency") or ""),
            "source_url": f"https://www.eia.gov/opendata/browser/petroleum",
        }
        return NormalizedObservation(
            source="eia", source_event_id=code,
            event_type="economic_indicator",
            occurred_at=latest.get("period") or "",
            source_updated_at=latest.get("period"),
            latitude=None, longitude=None, location_label="United States",
            title=f"{label}: {value:,.0f} {units} ({latest.get('period')})",
            severity=value, status="reported", reviewed=False,
            fields=fields, cross_references=[])
