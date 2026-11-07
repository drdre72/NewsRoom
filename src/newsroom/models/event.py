"""Canonical event model (§8).

One real-world incident == one canonical event. Source updates append
revisions; they never mint new events.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class EventStatus(str, Enum):
    ACTIVE = "active"
    MONITORING = "monitoring"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class ImpactLevel(str, Enum):
    FLASH = "FLASH"
    BREAKING = "BREAKING"
    UPDATE = "UPDATE"
    NORMAL = "NORMAL"
    BACKGROUND = "BACKGROUND"


@dataclass
class CanonicalEvent:
    event_id: str
    event_type: str                       # earthquake | severe_weather | ...
    status: str                         # EventStatus value
    severity: float                     # primary magnitude-class value (e.g. 6.4)
    confidence: float                   # confidence engine output 0..1
    first_seen: str                     # UTC ISO-8601
    last_updated: str
    latitude: float | None = None
    longitude: float | None = None
    location_label: str | None = None
    occurred_at: str | None = None        # event time per source (fusion clock)
    title: str | None = None
    impact_score: float = 0.0
    impact_level: str = ImpactLevel.BACKGROUND.value
    confidence_score: float = 0.0
    current_revision: int = 0
    source_ids: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EventRevision:
    event_id: str
    revision: int
    created_at: str
    snapshot: dict[str, Any]             # canonical field state at this revision
    changed_fields: list[str]          # fields that differed vs prior revision
    observation_id: str | None = None
