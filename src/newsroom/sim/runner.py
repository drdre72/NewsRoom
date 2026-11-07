"""Simulation runner (§51) — offline, deterministic, fixture-driven.

    python main.py simulate earthquake_major

Feed items replay through the REAL ingest path — the same code that handles
live polls — so a simulation exercises event creation, fusion, revision
detection, fact ledger, scoring, and (optionally) the full render.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from ..config import Settings
from ..ingestion.adapters.usgs import UsgsAdapter
from ..ingestion.adapters.nws import NwsAdapter
from ..ingestion.adapters.eonet import EonetAdapter
from ..logging import get_logger, log_event
from ..pipeline import ingest_observation
from ..storage import repositories as repo

log = get_logger("sim")

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIOS = Path(__file__).parent / "scenarios.yaml"

_ADAPTERS = {"usgs": UsgsAdapter, "nws": NwsAdapter, "eonet": EonetAdapter}


def list_scenarios() -> dict:
    return yaml.safe_load(SCENARIOS.read_text()) or {}


def run_scenario(
    conn: sqlite3.Connection, settings: Settings, name: str,
    dry_run: bool = False,
) -> dict:
    scenarios = list_scenarios()
    if name not in scenarios:
        raise ValueError(
            f"unknown scenario '{name}'. available: {sorted(scenarios)}")
    spec = scenarios[name]
    base = datetime.now(timezone.utc)
    results: dict = {"scenario": name, "items": [], "event_ids": []}
    last_event = None

    for item in spec.get("items", []):
        path = FIXTURES / item["file"]
        payload = path.read_text(encoding="utf-8").strip()
        adapter = _ADAPTERS[item["adapter"]](
            name=item["adapter"], config={})
        at = base + timedelta(seconds=float(item.get("at_offset_s", 0)))
        retrieved = at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        outcome = ingest_observation(
            conn, adapter, payload,
            feed_url=f"fixture://{item['file']}", retrieved_at=retrieved)
        results["items"].append({"file": item["file"], **{
            k: v for k, v in outcome.items() if k != "changed_fields"}})
        if outcome.get("event_id"):
            last_event = outcome["event_id"]
            if last_event not in results["event_ids"]:
                results["event_ids"].append(last_event)
        log_event(log, 20, "sim item", scenario=name, file=item["file"],
                  outcome={k: v for k, v in outcome.items() if k != "changed_fields"})

    if spec.get("render") and last_event:
        from ..render import render_short
        results["render"] = render_short(conn, settings, last_event,
                                         dry_run=dry_run)
    return results
