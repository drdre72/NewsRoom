"""FRED adapter — Federal Reserve Economic Data (stlouisfed.org/fred).

Each configured series is a stable `economic_indicator` event keyed by
series id; each new data release revises it. severity_norm is the
surprise: |pct change vs previous print| / per-series move_scale.
FRED_API_KEY env only.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Iterable

from .base import SourceAdapter, FetchResult
from ...models.observation import NormalizedObservation

_BASE = "https://api.stlouisfed.org/fred/series/observations"


class FredAdapter(SourceAdapter):
    kind = "fred"
    event_type = "economic_indicator"

    def fetch(self, timeout: int = 20, user_agent: str = "newsroom/0.1") -> FetchResult:
        from ...storage.repositories import utcnow
        key = os.environ.get("FRED_API_KEY", "")
        if not key:
            raise RuntimeError("FRED_API_KEY not set")
        payloads = []
        for sid in self.config.get("series", {}):
            q = urllib.parse.urlencode({
                "series_id": sid, "api_key": key, "file_type": "json",
                "sort_order": "desc", "limit": 2})
            url = f"{_BASE}?{q}"
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
            # Response carries no series id — tag it from the request.
            payloads.append(json.dumps(
                {"series_id": sid,
                 "observations": doc.get("observations", [])}))
        return FetchResult(feed_url=_BASE + "?[key]",
                           retrieved_at=utcnow(), payloads=payloads)

    def split_items(self, body: str) -> Iterable[str]:
        yield body                       # one payload per series already

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        doc = json.loads(raw_payload)
        sid = doc.get("series_id")
        obs = [o for o in (doc.get("observations") or [])
               if o.get("value") not in (None, ".")]
        if not obs or not sid:
            return None
        latest, prev = obs[0], (obs[1] if len(obs) > 1 else None)
        try:
            value = float(latest["value"])
        except (KeyError, TypeError, ValueError):
            return None
        meta = (self.config.get("series") or {}).get(sid, {}) or {}
        label = meta.get("label", sid or "FRED series")
        units = meta.get("units", "")
        scale = float(meta.get("move_scale", 2.0))
        delta = delta_pct = None
        if prev:
            try:
                pv = float(prev["value"])
                delta = value - pv
                delta_pct = (delta / pv * 100.0) if pv else 0.0
            except (TypeError, ValueError):
                pass
        snorm = min(1.0, abs(delta_pct or 0.0) / scale)
        fields = {
            "series_id": sid, "series_label": label, "units": units,
            "value_date": latest.get("date"),
            "previous_value": prev.get("value") if prev else None,
            "delta": delta, "delta_pct": delta_pct,
            "period_name": latest.get("date"),
            "category": label, "category_title": "Economic Data",
            "severity_norm": snorm, "magnitude_unit": units,
            "source_url": f"https://fred.stlouisfed.org/series/{sid}",
        }
        return NormalizedObservation(
            source="fred", source_event_id=str(sid),
            event_type="economic_indicator",
            occurred_at=latest.get("date") or "",
            source_updated_at=latest.get("realtime_start"),
            latitude=None, longitude=None, location_label="United States",
            title=f"{label}: {value} {units} ({latest.get('date')})",
            severity=value, status="reported", reviewed=False,
            fields=fields, cross_references=[])
