"""Fact ledger entries (§10).

Facts are append-only. When a value changes, the old fact is SUPERSEDED and
the new one becomes CURRENT — both stay queryable so reporting can describe
revisions honestly ("initially reported as 6.2, since revised to 6.4").
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


STATUS_CURRENT = "current"
STATUS_SUPERSEDED = "superseded"
STATUS_DISPUTED = "disputed"


@dataclass
class Fact:
    fact_id: str
    event_id: str
    revision: int
    field: str                           # magnitude | depth_km | location_label | …
    value: Any
    value_display: str                   # human-readable rendering
    source: str
    observation_id: str | None
    confidence: float
    status: str                          # current | superseded | disputed
    created_at: str
    superseded_by: str | None = None
