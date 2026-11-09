"""Crime desk adapter — attributed major-crime press coverage (§53).

Same RSS/Atom parsing as NewsRssAdapter, but emits `crime_report` events
so the rundown can schedule them under the top-priority Crime segment.

Severity is phrase-scored, not word-scored: the phrases that matter in
crime coverage ("mass shooting", "amber alert", "federal indictment")
are multi-word and can't be caught by the single-word boost table.
The Google News-backed instances are disabled for commercial-use rights.
Official DOJ releases now use this adapter's crime scoring and attribution
through DojPressAdapter, without an aggregation layer.
"""
from __future__ import annotations

from .news_rss import NewsRssAdapter

# Phrase -> severity_norm. Federal-grade breaking crime (manhunts, mass
# shootings, abductions) scores into the BREAKING-capable band; routine
# enforcement headlines score UPDATE/NORMAL.
_CRIME_PHRASES = {
    "mass shooting": 0.75, "active shooter": 0.75, "serial killer": 0.75,
    "hostage": 0.70, "manhunt": 0.70, "amber alert": 0.70,
    "school shooting": 0.70, "officer killed": 0.70,
    "terror attack": 0.70, "bombing": 0.70, "abducted": 0.65,
    "human trafficking": 0.65, "fbi raid": 0.65, "homicide": 0.60,
    "murder": 0.60, "cartel": 0.60, "missing child": 0.60,
    "kidnapp": 0.60, "shooting": 0.55, "fugitive": 0.55,
    "indict": 0.55, "death penalty": 0.55, "execution": 0.55,
    "raid": 0.50, "stabbing": 0.50, "charged": 0.50,
    "armed robbery": 0.45, "arrested": 0.45, "convicted": 0.45,
}


class CrimeRssAdapter(NewsRssAdapter):
    kind = "crime_rss"
    event_type = "crime_report"

    def _snorm(self, title: str) -> float:
        low = title.lower()
        hits = [s for p, s in _CRIME_PHRASES.items() if p in low]
        # Baseline 0.30 — items already passed a crime-filtered query.
        return max([0.30] + hits)
