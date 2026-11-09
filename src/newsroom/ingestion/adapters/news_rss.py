"""News RSS/Atom adapter — attributed press reports (§53).

Unlike sensor feeds (USGS/NWS), these are *reports by outlets*, not
measurements. Every normalized observation is a `news_report` event keyed
by article URL, scored low by default so press coverage enters the
rundown at UPDATE/NORMAL — it corroborates, it does not interrupt.

Formats: `rss` (RSS 2.0 + Atom via stdlib ElementTree) and `wp_json`
(WordPress REST `/wp/v2/posts` — for outlets with feeds disabled).
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from typing import Iterable

from .base import SourceAdapter, FetchResult
from ...models.observation import NormalizedObservation
from ...security import fetch_bytes

_ATOM = "{http://www.w3.org/2005/Atom}"
_TAG_RE = re.compile(r"<[^>]+>")
_MAX_SUMMARY = 400

# Headline keywords that lift a report above the baseline — world news
# weight, not verified severity.
_KEYWORD_BOOST = {
    "killed": 0.45, "dead": 0.45, "attack": 0.45, "missile": 0.45,
    "strike": 0.40, "war": 0.35, "ceasefire": 0.40, "hostage": 0.45,
    "earthquake": 0.40, "hurricane": 0.40, "tsunami": 0.45,
    "flood": 0.35, "explosion": 0.40, "crash": 0.35, "arrest": 0.30,
    "election": 0.30, "resign": 0.35, "coup": 0.50, "invasion": 0.50,
    "nuclear": 0.40, "sanctions": 0.25,
}


def _rfc2822(text: str) -> str:
    try:
        return email.utils.parsedate_to_datetime(text).isoformat()
    except (TypeError, ValueError):
        return text.strip()


def _iso_pub(text: str) -> str:
    try:
        return _rfc2822(text)
    except Exception:  # noqa: BLE001 — dates vary wildly across outlets
        return ""


def _clean(text: str | None) -> str:
    return _TAG_RE.sub(" ", text or "").replace("&amp;", "&").replace(
        "&lt;", "<").replace("&gt;", ">").replace("&#8217;", "'").replace(
        "&quot;", '"').replace("&#8220;", '"').replace("&#8221;", '"').strip()


class NewsRssAdapter(SourceAdapter):
    kind = "news_rss"
    event_type = "news_report"

    def _snorm(self, title: str) -> float:
        words = set(re.findall(r"[a-z]+", title.lower()))
        return max(0.25, *(_KEYWORD_BOOST.get(w, 0.0) for w in words))

    def fetch(self, timeout: int = 20, user_agent: str = "newsroom/0.1") -> FetchResult:
        from ...storage.repositories import utcnow
        url = self.feed_url()
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        body = fetch_bytes(req, timeout=timeout).decode("utf-8")
        if self.config.get("format") == "wp_json":
            payloads = self._wp_items(body)
        else:
            payloads = self._rss_items(body)
        return FetchResult(feed_url=url, retrieved_at=utcnow(),
                           payloads=payloads)

    # -- feed parsing ---------------------------------------------------
    def _rss_items(self, body: str) -> list[str]:
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return []
        out = []
        for item in root.iter("item"):
            link = item.findtext("link") or item.findtext("guid") or ""
            raw_title = _clean(item.findtext("title"))
            source_el = item.findtext("source") or ""
            # Google News titles end with " - Source"; strip it since we
            # carry the source separately and credit the original outlet.
            if " - " in raw_title and source_el:
                raw_title = raw_title.rsplit(" - ", 1)[0].strip()
            out.append({
                "title": raw_title,
                "link": link.strip(),
                "published": _iso_pub(item.findtext("pubDate") or ""),
                "summary": _clean(item.findtext("description"))[:_MAX_SUMMARY],
                "guid": (item.findtext("guid") or "").strip(),
                "source": source_el.strip(),
            })
        for entry in root.iter(f"{_ATOM}entry"):
            link = ""
            for ln in entry.findall(f"{_ATOM}link"):
                if ln.get("rel") in (None, "alternate"):
                    link = ln.get("href", "")
                    break
            out.append({
                "title": _clean(entry.findtext(f"{_ATOM}title")),
                "link": link.strip(),
                "published": _iso_pub(
                    entry.findtext(f"{_ATOM}published")
                    or entry.findtext(f"{_ATOM}updated") or ""),
                "summary": _clean(
                    entry.findtext(f"{_ATOM}summary")
                    or entry.findtext(f"{_ATOM}content"))[:_MAX_SUMMARY],
                "guid": (entry.findtext(f"{_ATOM}id") or "").strip(),
            })
        return [json.dumps(i) for i in out[: int(self.config.get("max_items", 25))]
                if i["title"] and (i["link"] or i["guid"])]

    def _wp_items(self, body: str) -> list[str]:
        try:
            posts = json.loads(body)
        except ValueError:
            return []
        out = []
        for p in posts[: int(self.config.get("max_items", 25))]:
            title = _clean((p.get("title") or {}).get("rendered"))
            link = p.get("link") or ""
            if title and link:
                out.append(json.dumps({
                    "title": title, "link": link,
                    "published": (p.get("date_gmt") or "") + "Z"
                                 if p.get("date_gmt") else "",
                    "summary": _clean((p.get("excerpt") or {}).get("rendered"))
                               [:_MAX_SUMMARY],
                    "guid": str(p.get("id", "")),
                }))
        return out

    def split_items(self, body: str) -> Iterable[str]:
        yield body                     # one payload per article already

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        item = json.loads(raw_payload)
        title = item.get("title", "").strip()
        link = item.get("link", "").strip()
        if not title:
            return None
        # Credit the original reporting source, not the aggregator.
        # Google News RSS carries a <source> element (Reuters, AP, etc.);
        # the configured outlet is the fallback for direct feeds (BBC, etc.).
        original_source = item.get("source", "").strip()
        outlet = original_source or self.config.get("outlet", self.name)
        key = hashlib.sha1(
            (item.get("guid") or link or title).encode()).hexdigest()[:16]
        snorm = self._snorm(title)
        fields = {
            "outlet": outlet,
            "headline": title,
            "report_url": link or item.get("guid", ""),
            "published_at": item.get("published", ""),
            "summary": item.get("summary", ""),
            "category": outlet, "category_title": "Press Report",
            "severity_norm": snorm,
        }
        return NormalizedObservation(
            source=self.name, source_event_id=f"{self.name}:{key}",
            event_type=self.event_type,
            occurred_at=item.get("published") or "",
            source_updated_at=item.get("published"),
            latitude=None, longitude=None,
            location_label=self.config.get("region", ""),
            title=f"{outlet}: {title}",
            severity=snorm, status="reported", reviewed=False,
            fields=fields, cross_references=[])
