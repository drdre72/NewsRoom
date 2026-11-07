"""Raw and normalized observations (§9).

Raw observations are immutable evidence — never overwritten, never deleted.
Normalization produces a typed view used for correlation and facts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawObservation:
    observation_id: str
    source: str                          # usgs | nws | eonet | gdacs | fixture
    source_event_id: str | None
    feed_url: str | None
    retrieved_at: str                    # UTC ISO-8601
    source_updated_at: str | None
    payload_hash: str
    raw_payload: str                     # verbatim JSON/text payload
    event_id: str | None = None          # filled once correlated


@dataclass
class NormalizedObservation:
    """Structured fields extracted from a raw payload by an adapter."""
    source: str
    source_event_id: str
    event_type: str
    occurred_at: str                     # event time per source
    source_updated_at: str | None
    latitude: float | None = None
    longitude: float | None = None
    location_label: str | None = None
    title: str | None = None
    severity: float | None = None        # primary scalar (magnitude, category…)
    status: str | None = None
    reviewed: bool = False
    fields: dict[str, Any] = field(default_factory=dict)   # event-type specifics
    cross_references: list[str] = field(default_factory=list)
