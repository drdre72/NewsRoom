"""Source polling (always-on, lightweight).

One poll cycle = fetch feed -> split items -> hand each raw payload to the
ingest pipeline. Payload dedup happens in storage (same hash -> no-op), so
re-polling an unchanged feed is cheap.
"""
from __future__ import annotations

import json
import sqlite3
import time

import truststore

from ..config import Settings
from ..logging import get_logger, log_event
from .adapters import adapter_for

log = get_logger("polling")
# Use macOS Keychain roots for verified HTTPS in the uv-managed Python.
# The default OpenSSL bundle can lack locally trusted network roots.
truststore.inject_into_ssl()
_last_successful_poll: dict[str, float] = {}


def enabled_sources(settings: Settings) -> list[tuple[str, dict]]:
    out = []
    for name, cfg in (settings.get("sources", {}) or {}).items():
        if isinstance(cfg, dict) and cfg.get("enabled"):
            out.append((name, cfg))
    return out


def poll_once(conn: sqlite3.Connection, settings: Settings, ingest) -> dict:
    """Run one poll cycle across enabled sources. `ingest` is the pipeline
    entry point: ingest(conn, adapter, raw_payload, feed_url, retrieved_at)."""
    timeout = int(settings.get("polling.timeout_seconds", 20))
    ua = settings.get("polling.user_agent", "newsroom/0.1")
    report: dict = {"sources": {}, "new_items": 0, "revisions": 0, "errors": []}
    for name, cfg in enabled_sources(settings):
        missing_env = [v for v in cfg.get("requires_env", [])
                       if not __import__("os").environ.get(v)]
        if missing_env:
            report["sources"][name] = {
                "ok": True, "skipped": "credentials",
                "missing_env": missing_env}
            continue
        interval = cfg.get("poll_every_seconds")
        if interval:
            # A successful fetch can contain zero items or only duplicates.
            # Observation age alone would then re-fetch every core tick.
            last_poll = _last_successful_poll.get(name)
            if last_poll is not None and time.monotonic() - last_poll < float(interval):
                report["sources"][name] = {
                    "ok": True, "skipped": "interval"}
                continue
            from ..intelligence.freshness import latest_observation_age
            age = latest_observation_age(conn, name)
            if age is not None and age < float(interval):
                report["sources"][name] = {
                    "ok": True, "skipped": "interval",
                    "age_seconds": round(age, 1)}
                continue
        try:
            adapter = adapter_for(cfg["kind"], name, cfg)
            result = adapter.fetch(
                timeout=int(cfg.get("timeout_seconds", timeout)),
                user_agent=ua)
            items = 0
            for body in result.payloads:
                for item in adapter.split_items(body):
                    items += 1
                    outcome = ingest(conn, adapter, item, result.feed_url,
                                     result.retrieved_at)
                    report["new_items"] += int(outcome.get("new_observation", 0))
                    report["revisions"] += int(outcome.get("new_revision", 0))
            _last_successful_poll[name] = time.monotonic()
            report["sources"][name] = {"items": items, "ok": True}
            log_event(log, 20, "poll complete", source=name, items=items,
                      feed=result.feed_url)
        except Exception as exc:  # noqa: BLE001 — one source failure ≠ outage
            report["sources"][name] = {"ok": False, "error": str(exc)}
            report["errors"].append({"source": name, "error": str(exc)})
            log_event(log, 40, "poll failed", source=name, error=str(exc))
    return report
