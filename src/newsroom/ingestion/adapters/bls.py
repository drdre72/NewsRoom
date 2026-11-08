"""BLS adapter — Bureau of Labor Statistics public API v2.

POST /publicAPI/v2/timeseries/data/ with seriesid list + registrationkey
(optional but rate-limited without). `latest: true` returns each series'
newest datapoint plus precomputed pct_changes — the surprise signal for
severity_norm. Shares `economic_indicator` with FRED (§53).
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Iterable

from .base import SourceAdapter, FetchResult
from ...models.observation import NormalizedObservation
from ...security import fetch_bytes

_API = "https://api.bls.gov/publicAPI/v2/timeseries/data/"


class BlsAdapter(SourceAdapter):
    kind = "bls"
    event_type = "economic_indicator"

    def fetch(self, timeout: int = 20, user_agent: str = "newsroom/0.1") -> FetchResult:
        from ...storage.repositories import utcnow
        body = {"seriesid": list(self.config.get("series", {})),
                "latest": True, "calculations": True}
        key = os.environ.get("BLS_API_KEY", "")
        if key:
            body["registrationkey"] = key
        req = urllib.request.Request(
            _API, data=json.dumps(body).encode(),
            headers={"User-Agent": user_agent,
                     "Content-Type": "application/json"})
        return FetchResult(feed_url=_API, retrieved_at=utcnow(),
                           payloads=[fetch_bytes(req, timeout=timeout).decode("utf-8")])

    def split_items(self, body: str) -> Iterable[str]:
        doc = json.loads(body)
        for row in (doc.get("Results") or {}).get("series", []) or []:
            yield json.dumps(row, separators=(",", ":"), sort_keys=True)

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        row = json.loads(raw_payload)
        sid = row.get("seriesID")
        data = [d for d in (row.get("data") or [])
                if d.get("value") not in (None, "")]
        if not sid or not data:
            return None
        latest = data[0]
        try:
            value = float(latest["value"].replace(",", ""))
        except (KeyError, TypeError, ValueError):
            return None
        meta = (self.config.get("series") or {}).get(sid, {}) or {}
        label = meta.get("label", sid)
        units = meta.get("units", "")
        scale = float(meta.get("move_scale", 2.0))
        calcs = latest.get("calculations") or {}
        pcts = calcs.get("pct_changes") or {}
        nets = calcs.get("net_changes") or {}
        delta_pct = None
        for k in ("1", "3", "6", "12"):
            if pcts.get(k) not in (None, ""):
                try:
                    delta_pct = float(str(pcts[k]).replace(",", ""))
                except ValueError:
                    pass
                break
        snorm = min(1.0, abs(delta_pct or 0.0) / scale)
        period = f"{latest.get('periodName','')} {latest.get('year','')}".strip()
        fields = {
            "series_id": sid, "series_label": label, "units": units,
            "value_date": period,
            "previous_value": None,
            "delta": nets.get("1"), "delta_pct": delta_pct,
            "period_name": period,
            "category": label, "category_title": "Economic Data",
            "severity_norm": snorm, "magnitude_unit": units,
            "source_url": f"https://data.bls.gov/timeseries/{sid}",
        }
        return NormalizedObservation(
            source="bls", source_event_id=str(sid),
            event_type="economic_indicator",
            occurred_at=str(latest.get("year") or ""),
            source_updated_at=None,
            latitude=None, longitude=None, location_label="United States",
            title=f"{label}: {value} {units} ({period})",
            severity=value, status="reported", reviewed=False,
            fields=fields, cross_references=[])
