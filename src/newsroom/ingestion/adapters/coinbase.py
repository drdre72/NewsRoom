"""Coinbase adapter — cryptocurrency spot price snapshots.

Polls Coinbase v2 spot prices for configured pairs (BTC-USD, ETH-USD,
etc.) as `market_quote` events — each pair is a stable event keyed by
the pair string; a changed quote revises it. Atomic ingestion computes
severity from the absolute change vs the previous stored quote.
Public API — no key required; the feed carries no exchange timestamp.
"""
from __future__ import annotations

import json
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

from .base import SourceAdapter, FetchResult
from ...models.observation import NormalizedObservation
from ...security import fetch_bytes

_BASE = "https://api.coinbase.com/v2/prices"


class CoinbaseAdapter(SourceAdapter):
    kind = "coinbase"
    event_type = "market_quote"

    def fetch(self, timeout: int = 20, user_agent: str = "newsroom/0.1") -> FetchResult:
        from ...storage.repositories import utcnow
        pairs = self.config.get("pairs", {})
        payloads = []
        for pair, meta in pairs.items():
            url = f"{_BASE}/{pair}/spot"
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            doc = json.loads(fetch_bytes(req, timeout=timeout).decode("utf-8"))
            payloads.append(json.dumps(
                {"pair": pair,
                 "label": meta.get("label", pair),
                 "move_scale": meta.get("move_scale", 5.0),
                 "data": doc.get("data", {})}))
        return FetchResult(feed_url=_BASE, retrieved_at=utcnow(),
                           payloads=payloads)

    def split_items(self, body: str) -> Iterable[str]:
        yield body

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        doc = json.loads(raw_payload)
        pair = doc.get("pair")
        data = doc.get("data") or {}
        amount = data.get("amount")
        if not pair or not amount:
            return None
        try:
            value = float(Decimal(str(amount)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if value <= 0:
            return None
        label = doc.get("label", pair)
        # The previous quote is database state, not part of the feed. The
        # atomic ingestion path derives the move before it scores the event.
        fields = {
            "pair": pair, "series_label": label,
            "units": data.get("currency", "USD"),
            "series_id": pair, "magnitude_unit": data.get("currency", "USD"),
            "move_scale": float(doc.get("move_scale", 5.0)),
            "category": label, "category_title": "Crypto Markets",
            "source_url": f"https://www.coinbase.com/price/{pair.split('-')[0].lower()}",
        }
        return NormalizedObservation(
            source="coinbase", source_event_id=pair,
            event_type="market_quote",
            occurred_at="",            # spot — no historical date
            source_updated_at=None,
            latitude=None, longitude=None, location_label="Global",
            title=f"{label}: ${value:,.2f} ({pair})",
            severity=value, status="live", reviewed=False,
            fields=fields, cross_references=[])
