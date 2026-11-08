"""ACLED adapter — Armed Conflict Location & Event Data (acleddata.com).

Curated conflict/protest events with actor, location, and fatality fields.
Credentials are env-only (ACLED_API_KEY + ACLED_EMAIL); the poller marks the
source "skipped: credentials" while they're unset.

Severity is `severity_norm`: min(1, fatalities/50) — precomputed here because
this family's unit differs from GDELT's on the shared conflict_event type.
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
from .nws import _iso

_READ_URL = "https://api.acleddata.com/acled/read"
_PROTEST_TYPES = {"Protests", "Riots"}


class AcledAdapter(SourceAdapter):
    kind = "acled"
    event_type = "conflict_event"     # protests get their own type below

    def fetch(self, timeout: int = 20, user_agent: str = "newsroom/0.1") -> FetchResult:
        from ...storage.repositories import utcnow
        key = os.environ.get("ACLED_API_KEY", "")
        email = os.environ.get("ACLED_EMAIL", "")
        if not key or not email:
            raise RuntimeError("ACLED_API_KEY/ACLED_EMAIL not set")
        q = {"key": key, "email": email,
             "limit": int(self.config.get("limit", 200)),
             "event_date": self.config.get("event_date", ""),
             "event_date_where": ">=",
             "fields": "|".join([
                 "event_id_cnty", "event_date", "disorder_type",
                 "event_type", "sub_event_type", "actor1", "actor2",
                 "admin1", "country", "location", "latitude", "longitude",
                 "geo_precision", "source", "source_scale", "notes",
                 "fatalities", "timestamp", "region"])}
        for opt in ("region", "country", "event_type"):
            if self.config.get(opt):
                q[opt] = self.config[opt]
        url = f"{_READ_URL}?{urllib.parse.urlencode(q)}"
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        body = fetch_bytes(req, timeout=timeout).decode("utf-8")
        return FetchResult(feed_url=url.split("?")[0] + "?[key+email]",
                           retrieved_at=utcnow(), payloads=[body])

    def split_items(self, body: str) -> Iterable[str]:
        doc = json.loads(body)
        for row in doc.get("data", []) or []:
            yield json.dumps(row, separators=(",", ":"), sort_keys=True)

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        row = json.loads(raw_payload)
        event_id = row.get("event_id_cnty")
        if not event_id:
            return None
        acled_type = row.get("event_type") or ""
        event_type = ("protest" if acled_type in _PROTEST_TYPES
                      else "conflict_event")
        try:
            fatalities = float(row.get("fatalities") or 0)
        except (TypeError, ValueError):
            fatalities = 0.0
        place = row.get("location") or row.get("admin1") or row.get("country")
        fields = {
            "disorder_type": row.get("disorder_type"),
            "acled_type": acled_type,
            "sub_event_type": row.get("sub_event_type"),
            "actor1": row.get("actor1"),
            "actor2": row.get("actor2"),
            "admin1": row.get("admin1"),
            "country": row.get("country"),
            "region": row.get("region"),
            "geo_precision": row.get("geo_precision"),
            "source_scale": row.get("source_scale"),
            "description": row.get("notes"),
            "severity_norm": min(1.0, fatalities / 50.0),
            "fatalities": fatalities,     # §15 ledgerable — citable field
            "magnitude_unit": "fatalities",
            "category": acled_type,
            "category_title": ("Protest" if event_type == "protest"
                               else "Conflict"),
        }
        return NormalizedObservation(
            source="acled",
            source_event_id=str(event_id),
            event_type=event_type,
            occurred_at=_iso(row.get("event_date")) or "",
            source_updated_at=str(row.get("timestamp") or "") or None,
            latitude=_f(row.get("latitude")),
            longitude=_f(row.get("longitude")),
            location_label=f"{place}, {row['country']}" if (
                place and row.get("country") and place != row["country"]
            ) else (place or row.get("country")),
            title=(f"{row.get('sub_event_type') or acled_type} — "
                   f"{place or row.get('country') or event_id}"),
            severity=fatalities or None,
            status="reported",
            reviewed=False,
            fields=fields,
            cross_references=([row["source"]] if row.get("source") else []),
        )


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
