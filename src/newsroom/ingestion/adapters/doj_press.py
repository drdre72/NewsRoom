"""Official DOJ press releases; report allegations as DOJ statements."""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .base import FetchResult
from .crime_rss import CrimeRssAdapter
from ...security import fetch_bytes
from ...storage.repositories import utcnow


class DojPressAdapter(CrimeRssAdapter):
    kind = "doj_press"

    def fetch(self, timeout: int = 20, user_agent: str = "newsroom/0.1") -> FetchResult:
        url = self.feed_url()
        body = fetch_bytes(urllib.request.Request(
            url, headers={"User-Agent": user_agent}), timeout=timeout)
        entries = json.loads(body).get("results", [])
        if not isinstance(entries, list):
            raise ValueError("DOJ releases response has no results list")
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        payloads = []
        for entry in entries[:int(self.config.get("max_items", 20))]:
            if not isinstance(entry, dict):
                continue
            try:
                published = datetime.fromtimestamp(int(entry["date"]), timezone.utc)
                parsed = urlsplit(entry.get("url", ""))
                port = parsed.port
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
            link = entry.get("url", "")
            if (published < cutoff or published > datetime.now(timezone.utc) or
                    parsed.scheme != "https" or
                    parsed.hostname not in ("www.justice.gov", "justice.gov") or
                    parsed.username or parsed.password or
                    port not in (None, 443) or parsed.fragment or
                    not entry.get("uuid") or not entry.get("title")):
                continue
            payloads.append(json.dumps({
                "title": entry["title"].strip(),
                "link": link,
                "guid": entry["uuid"],
                "published": published.isoformat(),
                "summary": "",
            }))
        return FetchResult(feed_url=url, retrieved_at=utcnow(), payloads=payloads)
