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
from ..security import redact_error
from .adapters import adapter_for

log = get_logger("polling")
# Use macOS Keychain roots for verified HTTPS in the uv-managed Python.
# The default OpenSSL bundle can lack locally trusted network roots.
truststore.inject_into_ssl()
_last_successful_poll: dict[str, float] = {}
_last_attempt: dict[str, float] = {}
_consecutive_failures: dict[str, int] = {}

# A failed source must not retry every cycle — that hammers a down endpoint
# (and spams logs) with no benefit. Back off 5m -> 10m -> 20m ... capped at
# 30m; the normal poll interval still governs healthy sources.
_FAILURE_BASE_S = 300.0
_FAILURE_CAP_S = 1800.0


def _failure_wait(name: str) -> float:
    fails = _consecutive_failures.get(name, 0)
    if not fails:
        return 0.0
    return min(_FAILURE_BASE_S * 2 ** (fails - 1), _FAILURE_CAP_S)


def enabled_sources(settings: Settings) -> list[tuple[str, dict]]:
    out = []
    for name, cfg in (settings.get("sources", {}) or {}).items():
        if isinstance(cfg, dict) and cfg.get("enabled"):
            out.append((name, cfg))
    return out


def poll_once(conn: sqlite3.Connection, settings: Settings, ingest) -> dict:
    """Run one poll cycle across enabled sources. `ingest` is the pipeline
    entry point: ingest(conn, adapter, raw_payload, feed_url, retrieved_at).

    Fetch is network I/O — sources are fetched concurrently so one slow
    feed can't head-of-line block the cycle. Ingest stays serial on the
    main thread in config order: one SQLite writer, deterministic order
    (§75).
    """
    timeout = int(settings.get("polling.timeout_seconds", 20))
    ua = settings.get("polling.user_agent", "newsroom/0.1")
    report: dict = {"sources": {}, "new_items": 0, "revisions": 0, "errors": []}

    # Phase 1 — gates: credentials, poll interval, failure backoff.
    pending: list[tuple[str, dict]] = []
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
        wait = _failure_wait(name)
        if wait and time.monotonic() - _last_attempt.get(name, 0.0) < wait:
            report["sources"][name] = {
                "ok": True, "skipped": "backoff",
                "fails": _consecutive_failures[name],
                "retry_in_s": round(
                    wait - (time.monotonic() - _last_attempt[name]))}
            continue
        _last_attempt[name] = time.monotonic()
        pending.append((name, cfg))

    # Phase 2 — concurrent fetch: adapters and result objects only; no
    # shared state, no db access in worker threads.
    fetched: dict[str, tuple] = {}
    if pending:
        from concurrent.futures import ThreadPoolExecutor

        def _fetch(name, cfg):
            adapter = adapter_for(cfg["kind"], name, cfg)
            return adapter, adapter.fetch(
                timeout=int(cfg.get("timeout_seconds", timeout)),
                user_agent=ua)

        with ThreadPoolExecutor(
                max_workers=min(8, len(pending))) as pool:
            futures = {name: pool.submit(_fetch, name, cfg)
                       for name, cfg in pending}
            for name, fut in futures.items():
                try:
                    fetched[name] = ("ok", *fut.result())
                except Exception as exc:  # noqa: BLE001 — collected, not raised
                    fetched[name] = ("err", exc)

    # Phase 3 — serial ingest, config order preserved.
    for name, cfg in pending:
        state, *rest = fetched[name]
        if state == "err":
            _consecutive_failures[name] = _consecutive_failures.get(name, 0) + 1
            error = redact_error(rest[0])
            report["sources"][name] = {"ok": False, "error": error}
            report["errors"].append({"source": name, "error": error})
            log_event(log, 40, "poll failed", source=name, error=error)
            continue
        adapter, result = rest
        try:
            items = 0
            for body in result.payloads:
                for item in adapter.split_items(body):
                    items += 1
                    outcome = ingest(conn, adapter, item, result.feed_url,
                                     result.retrieved_at)
                    report["new_items"] += int(outcome.get("new_observation", 0))
                    report["revisions"] += int(outcome.get("new_revision", 0))
            _last_successful_poll[name] = time.monotonic()
            _consecutive_failures.pop(name, None)
            report["sources"][name] = {"items": items, "ok": True}
            log_event(log, 20, "poll complete", source=name, items=items,
                      feed=result.feed_url)
        except Exception as exc:  # noqa: BLE001 — one source failure ≠ outage
            _consecutive_failures[name] = _consecutive_failures.get(name, 0) + 1
            error = redact_error(exc)
            report["sources"][name] = {"ok": False, "error": error}
            report["errors"].append({"source": name, "error": error})
            log_event(log, 40, "poll failed", source=name, error=error)
    return report
