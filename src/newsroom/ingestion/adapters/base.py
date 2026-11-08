"""Source adapter contract.

An adapter knows how to (a) fetch raw payloads from an authoritative source
and (b) normalize one payload into NormalizedObservation fields. Adapters
never decide event identity — that's the correlator's job.
"""
from __future__ import annotations

import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

from ...models.observation import NormalizedObservation
from ...security import fetch_bytes


@dataclass
class FetchResult:
    feed_url: str
    retrieved_at: str
    payloads: list[str] = field(default_factory=list)   # raw per-item payloads
    source_updated_at: str | None = None                # feed-level timestamp


class SourceAdapter:
    kind = "base"
    event_type = "unknown"

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config

    def feed_url(self) -> str:
        feeds = self.config.get("feeds", {})
        return feeds.get(self.config.get("poll_feed", ""), next(iter(feeds.values()), ""))

    def fetch(self, timeout: int = 20, user_agent: str = "newsroom/0.1") -> FetchResult:
        """HTTP GET the poll feed. Live path — never used by offline tests."""
        from ...storage.repositories import utcnow
        url = self.feed_url()
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        body = fetch_bytes(req, timeout=timeout).decode("utf-8")
        return FetchResult(feed_url=url, retrieved_at=utcnow(), payloads=[body])

    def split_items(self, body: str) -> Iterable[str]:
        """Split a fetched feed into per-item raw payloads."""
        return [body]

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        """Parse one raw payload into normalized fields. None = skip item."""
        raise NotImplementedError
