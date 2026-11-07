"""Fact Ledger (§10) — the authoritative basis for generated language.

Append-only per (event, field): a changed value supersedes the previous fact
and appends a new CURRENT one. History is never deleted, which is what makes
"initially reported X, since revised to Y" derivable from the ledger.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..ingestion.normalizer import display_value
from ..models.fact import Fact, STATUS_CURRENT, STATUS_SUPERSEDED
from ..storage import repositories as repo


# Which canonical fields become ledger facts. Anything the newsroom might say
# aloud must be represented here — otherwise scripts can't cite it.
LEDGER_FIELDS = (
    "title", "status", "severity", "magnitude", "mag_type",
    "latitude", "longitude", "depth_km", "location_label",
    "occurred_at", "tsunami", "alert", "sig", "mmi", "felt",
    # weather_alert (NWS CAP)
    "alert_event", "severity_text", "certainty", "urgency", "response",
    "sender_name", "headline", "description", "instruction", "motion",
    "effective", "onset", "expires", "ends", "message_type", "awips_id",
    # EONET natural events
    "category", "category_title", "closed", "closed_at", "magnitude_unit",
    "source_url", "eonet_link",
    # GDACS + SWPC
    "alert_level", "gdacs_type", "country", "population",
    "swpc_code", "noaa_scale", "product_id",
)


class FactLedger:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def apply_snapshot(
        self,
        event_id: str,
        revision: int,
        snapshot: dict[str, Any],
        source: str,
        observation_id: str,
        confidence: float,
    ) -> list[Fact]:
        """Record a revision's fields. Returns newly written CURRENT facts."""
        written: list[Fact] = []
        current = {f.field: f for f in repo.current_facts(self.conn, event_id)}
        for field in LEDGER_FIELDS:
            if field not in snapshot or snapshot[field] is None:
                continue
            value = snapshot[field]
            old = current.get(field)
            if old and old.value == value:
                continue  # unchanged — keep the original fact
            fact = Fact(
                fact_id=repo.new_id("f"), event_id=event_id, revision=revision,
                field=field, value=value, value_display=display_value(field, value),
                source=source, observation_id=observation_id,
                confidence=confidence, status=STATUS_CURRENT,
                created_at=repo.utcnow(),
            )
            if old:
                repo.supersede_fact(self.conn, old.fact_id, fact.fact_id)
                fact.superseded_by = None
            repo.insert_fact(self.conn, fact)
            written.append(fact)
        return written

    def current(self, event_id: str) -> list[Fact]:
        return repo.current_facts(self.conn, event_id)

    def history(self, event_id: str, field: str) -> list[Fact]:
        return repo.facts_for_field(self.conn, event_id, field)

    def revision_summary(self, event_id: str, field: str) -> dict | None:
        """old→new pair for "initially reported X, since revised to Y"."""
        hist = self.history(event_id, field)
        superseded = [f for f in hist if f.status == STATUS_SUPERSEDED]
        current = [f for f in hist if f.status == STATUS_CURRENT]
        if not superseded or not current:
            return None
        return {
            "field": field,
            "initial": superseded[0].value_display,
            "current": current[-1].value_display,
            "initial_fact_id": superseded[0].fact_id,
            "current_fact_id": current[-1].fact_id,
        }
