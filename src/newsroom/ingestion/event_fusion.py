"""Cross-source event fusion (§16).

Two observations describe the same real-world incident when they agree on
event type AND fall within a configurable time window AND sit within a
configurable distance. Deterministic and explainable — every merge is
recorded in event_sources.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from ..models.observation import NormalizedObservation

_PRESS_TYPES = {"news_report", "crime_report"}
# Outlet names + generic news verbs must not count toward sameness —
# "Google News: X" vs "Google News: Y" would otherwise share tokens.
_HEADLINE_STOP = {
    "with", "from", "that", "this", "have", "has", "been", "were",
    "they", "their", "after", "amid", "over", "says", "said", "what",
    "when", "more", "than", "into", "some", "also", "will", "would",
    "about", "google", "news", "reports", "report", "police",
}


def _headline_tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 3 and w not in _HEADLINE_STOP}


def _headline_similarity(obs, candidate) -> float:
    """Jaccard overlap between the raw incoming headline and the stored
    event headline — catches the same story carried by many outlets."""
    a = _headline_tokens(obs.fields.get("headline") or obs.title)
    cand_head = (getattr(candidate, "extra", {}) or {}).get("headline") \
        or getattr(candidate, "title", "")
    b = _headline_tokens(str(cand_head))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def time_gap_seconds(a: str | None, b: str | None) -> float | None:
    da, db = _parse_iso(a), _parse_iso(b)
    if not da or not db:
        return None
    return abs((da - db).total_seconds())


def should_fuse(
    obs: NormalizedObservation,
    candidate: Any,
    time_window_s: float,
    max_distance_km: float,
) -> tuple[bool, str]:
    """Deterministic fusion predicate with an explanation string."""
    if obs.event_type != candidate.event_type:
        return False, "different event_type"
    # Press reports have no geo and publish over hours — geo/time fusion
    # can never merge the same story across outlets. Headline-token
    # overlap is the real identity signal: wire-copy duplicates fuse,
    # genuinely different angles stay separate events.
    if obs.event_type in _PRESS_TYPES:
        sim = _headline_similarity(obs, candidate)
        if sim >= 0.40:
            return True, f"same-story headline ({sim:.2f} token overlap)"
        return False, f"press: different story ({sim:.2f} overlap)"
    # Same-family sub-type discriminator: a Tornado Warning and a Flood Watch
    # for the same county are different incidents, however near in time/space.
    if obs.event_type == "weather_alert":
        alert = (obs.fields.get("alert_event") or "").lower()
        cand_alert = str(
            getattr(candidate, "extra", {}).get("alert_event") or "").lower()
        if alert and cand_alert and alert != cand_alert:
            return False, f"different alert type '{alert}' vs '{cand_alert}'"
    # Economic series share a release period (occurred_at) and carry no geo —
    # without this guard every same-period print fuses into one event. An
    # unset candidate series also vetoes: same-series corroboration is the
    # only fusion this family ever wants.
    if obs.event_type in ("economic_indicator", "market_quote"):
        sid = (obs.fields.get("series_id") or "").lower()
        cand_sid = str(
            getattr(candidate, "extra", {}).get("series_id") or "").lower()
        if sid and sid != cand_sid:
            return False, \
                f"different series '{sid}' vs '{cand_sid or 'unset'}'"
    if obs.event_type == "tropical_cyclone":
        name = str(obs.fields.get("storm_name") or "").casefold()
        candidate_name = str(
            getattr(candidate, "extra", {}).get("storm_name") or "").casefold()
        if name and candidate_name and name != candidate_name:
            return False, "different named storms"
    gap = time_gap_seconds(obs.occurred_at, candidate.occurred_at or candidate.first_seen)
    if gap is not None and gap > time_window_s:
        return False, f"time gap {gap:.0f}s > {time_window_s:.0f}s"
    if (obs.latitude is not None and obs.longitude is not None
            and candidate.latitude is not None and candidate.longitude is not None):
        d = haversine_km(obs.latitude, obs.longitude, candidate.latitude, candidate.longitude)
        if d > max_distance_km:
            return False, f"distance {d:.0f}km > {max_distance_km:.0f}km"
        return True, f"same type; gap={gap if gap is None else round(gap)}s; distance={d:.1f}km"
    # Missing coordinates on either side — only fuse on a strong time match.
    if gap is not None and gap <= time_window_s / 4:
        return True, f"same type; gap={round(gap)}s; coords missing on one side"
    return False, "insufficient agreement"
