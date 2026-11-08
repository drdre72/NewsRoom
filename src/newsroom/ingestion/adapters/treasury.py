"""Treasury FiscalData adapter — US Treasury monthly average interest rates.

Monthly average rates on marketable Treasury securities (Bills, Notes,
Bonds, TIPS) as `economic_indicator` events — each security type is a
stable event keyed by its description; new monthly prints revise it.
severity_norm = |rate change vs previous month| / move_scale. Public
API — no key required.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Iterable

from .base import SourceAdapter, FetchResult
from ...models.observation import NormalizedObservation
from ...security import fetch_bytes

_BASE = ("https://api.fiscaldata.treasury.gov/services/api/"
         "fiscal_service/v2/accounting/od/avg_interest_rates")


class TreasuryAdapter(SourceAdapter):
    kind = "treasury"
    event_type = "economic_indicator"

    def fetch(self, timeout: int = 20, user_agent: str = "newsroom/0.1") -> FetchResult:
        from ...storage.repositories import utcnow
        securities = self.config.get("securities", {})
        payloads = []
        for sec_desc, meta in securities.items():
            q = urllib.parse.urlencode({
                "fields": "security_desc,avg_interest_rate_amt,record_date",
                "filter": f"security_desc:eq:{sec_desc}",
                "sort": "-record_date",
                "pageSize": "2",
            })
            url = f"{_BASE}?{q}"
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            doc = json.loads(fetch_bytes(req, timeout=timeout).decode("utf-8"))
            rows = doc.get("data") or []
            payloads.append(json.dumps(
                {"security_desc": sec_desc,
                 "label": meta.get("label", sec_desc),
                 "move_scale": meta.get("move_scale", 1.0),
                 "rows": rows}))
        return FetchResult(feed_url=_BASE, retrieved_at=utcnow(),
                           payloads=payloads)

    def split_items(self, body: str) -> Iterable[str]:
        yield body

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        doc = json.loads(raw_payload)
        sec_desc = doc.get("security_desc")
        rows = doc.get("rows") or []
        if not sec_desc or not rows:
            return None
        latest = rows[0]
        prev = rows[1] if len(rows) > 1 else None
        try:
            value = float(latest["avg_interest_rate_amt"])
        except (KeyError, TypeError, ValueError):
            return None
        label = doc.get("label", sec_desc)
        scale = float(doc.get("move_scale", 1.0))
        delta = delta_pct = previous_rate = None
        if prev:
            try:
                pv = float(prev["avg_interest_rate_amt"])
                delta = round(value - pv, 4)
                delta_pct = round((delta / pv * 100.0) if pv else 0.0, 2)
                previous_rate = f"{pv:.3f}"
            except (TypeError, ValueError):
                pass
        snorm = min(1.0, abs(delta or 0.0) / scale) if scale > 0 else 0.0
        code = sec_desc.replace(" ", "_").replace("(", "").replace(")", "")
        fields = {
            "series_id": code, "series_label": label, "units": "percent",
            "value_date": latest.get("record_date"),
            "previous_value": prev.get("avg_interest_rate_amt") if prev else None,
            "delta": delta, "delta_pct": delta_pct,
            "rate_value": f"{value:.3f}",
            "previous_rate": previous_rate,
            "period_name": latest.get("record_date"),
            "category": label, "category_title": "Treasury Monthly Rates",
            "severity_norm": snorm, "magnitude_unit": "percent",
            "source_url": "https://fiscaldata.treasury.gov/datasets/"
                          "average-interest-rates-treasury-securities/",
        }
        return NormalizedObservation(
            source="treasury", source_event_id=code,
            event_type="economic_indicator",
            occurred_at=latest.get("record_date") or "",
            source_updated_at=latest.get("record_date"),
            latitude=None, longitude=None,
            location_label="United States",
            title=f"US Treasury {label}: {value:.3f}% ({latest.get('record_date')})",
            severity=value, status="reported", reviewed=True,
            fields=fields, cross_references=[])
