"""CISA KEV adapter — Known Exploited Vulnerabilities catalog.

The catalog is one static JSON file; each new addition is a `cyber_advisory`
event keyed by cveID. A lookback_days filter keeps first poll from
backfilling the whole catalog — only recent additions emit items.
severity_norm: baseline for active exploitation, +ransomware campaign use,
+imminent federal remediation deadline. Public feed — no key required.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .base import SourceAdapter, FetchResult
from ...models.observation import NormalizedObservation


class CisaAdapter(SourceAdapter):
    kind = "cisa_kev"
    event_type = "cyber_advisory"

    def fetch(self, timeout: int = 20, user_agent: str = "newsroom/0.1") -> FetchResult:
        from ...storage.repositories import utcnow
        url = self.feed_url()
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
        lookback = int(self.config.get("lookback_days", 2))
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=lookback)
        payloads = []
        for v in doc.get("vulnerabilities") or []:
            try:
                added = datetime.fromisoformat(v.get("dateAdded", "")).date()
            except ValueError:
                continue
            if added >= cutoff:
                payloads.append(json.dumps(v))
        return FetchResult(feed_url=url, retrieved_at=utcnow(),
                           payloads=payloads)

    def split_items(self, body: str) -> Iterable[str]:
        yield body                       # one payload per CVE already

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        v = json.loads(raw_payload)
        cve = v.get("cveID")
        name = v.get("vulnerabilityName")
        if not cve or not name:
            return None
        ransomware = v.get("knownRansomwareCampaignUse") == "Known"
        snorm = 0.55 + (0.3 if ransomware else 0.0)
        due = v.get("dueDate") or ""
        try:
            days_left = (datetime.fromisoformat(due).date()
                         - datetime.now(timezone.utc).date()).days
            if days_left <= 3:
                snorm += 0.1
        except ValueError:
            pass
        fields = {
            "cve_id": cve, "vendor": v.get("vendorProject") or "",
            "product": v.get("product") or "",
            "vuln_name": name,
            "short_description": (v.get("shortDescription") or "")[:400],
            "due_date": due,
            "ransomware_use": v.get("knownRansomwareCampaignUse") or "Unknown",
            "category": "known exploited" if ransomware
                        else "actively exploited",
            "category_title": "CISA Known Exploited Vulnerabilities",
            "severity_norm": min(1.0, snorm), "magnitude_unit": "CVE",
            "source_url": f"https://nvd.nist.gov/vuln/detail/{cve}",
        }
        return NormalizedObservation(
            source="cisa_kev", source_event_id=cve,
            event_type="cyber_advisory",
            occurred_at=v.get("dateAdded") or "",
            source_updated_at=v.get("dateAdded"),
            latitude=None, longitude=None, location_label="United States",
            title=f"{cve}: {name}",
            severity=min(1.0, snorm), status="active",
            reviewed=True, fields=fields, cross_references=[])
