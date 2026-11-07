"""Ingest pipeline — the deterministic core.

    raw payload
      -> RawObservation (immutable evidence, dedup by hash)
      -> NormalizedObservation (adapter)
      -> canonical event lookup: (source,source_event_id) else fusion match
      -> new event OR revision diff
      -> fact ledger apply (supersede changed fields)
      -> impact + confidence scoring
      -> story jobs

Everything here is deterministic code. No LLM involvement — the model only
ever *explains* the state this module establishes.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .config import Settings, load_settings
from .ingestion.adapters.base import SourceAdapter
from .ingestion.event_fusion import should_fuse
from .ingestion.normalizer import canonical_fields
from .ingestion.revision_tracker import diff_snapshots
from .intelligence.confidence_engine import production_gate, score_confidence
from .intelligence.corroboration import corroborating_source_count
from .intelligence.fact_ledger import FactLedger
from .intelligence.impact_engine import score_impact
from .logging import get_logger, log_event
from .models.event import CanonicalEvent, EventRevision, EventStatus
from .models.job import Job, JobStatus, JobType, Priority
from .models.observation import RawObservation
from .storage import repositories as repo

log = get_logger("pipeline")


def canonical_event_id(event_type: str, primary_source_event_id: str) -> str:
    return f"{event_type}:{primary_source_event_id}"


def _correlate(
    conn: sqlite3.Connection, obs, settings: Settings
) -> tuple[CanonicalEvent | None, str]:
    """Find the canonical event for this observation.

    Order: (1) exact source id lookup, (2) cross-source fusion match.
    Returns (event or None, reason).
    """
    existing = repo.get_event_by_source_id(conn, obs.source, obs.source_event_id)
    if existing:
        return existing, "source id match"
    since = _window_start(obs.occurred_at, settings)
    for cand in repo.candidate_events_for_fusion(conn, obs.event_type, since):
        fuse, why = should_fuse(
            obs, cand,
            float(settings.get("fusion.time_window_seconds", 300)),
            float(settings.get("fusion.max_distance_km", 50.0)),
        )
        if fuse:
            return cand, f"fusion: {why}"
    return None, "no match"


def _window_start(occurred_at: str, settings: Settings) -> str:
    from datetime import datetime, timedelta, timezone
    window = float(settings.get("fusion.time_window_seconds", 300)) * 4
    try:
        dt = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        dt = datetime.now(timezone.utc)
    return (dt - timedelta(seconds=window)).isoformat().replace("+00:00", "Z")


def ingest_observation(
    conn: sqlite3.Connection,
    adapter: SourceAdapter,
    raw_payload: str,
    feed_url: str | None,
    retrieved_at: str,
) -> dict[str, Any]:
    """One raw item through the whole deterministic pipeline."""
    outcome: dict[str, Any] = {
        "new_observation": 0, "new_event": 0, "new_revision": 0,
        "merged": 0, "facts_written": 0, "event_id": None,
    }
    obs_hash = repo.payload_hash(raw_payload)
    if repo.observation_hash_seen(conn, obs_hash):
        outcome["skipped"] = "duplicate payload"
        # Still resolve the canonical event so callers (e.g. sim reruns,
        # render requests) can act on already-ingested state.
        try:
            norm0 = adapter.normalize(raw_payload)
        except Exception:  # noqa: BLE001
            norm0 = None
        if norm0:
            existing = repo.get_event_by_source_id(
                conn, norm0.source, norm0.source_event_id)
            if existing:
                outcome["event_id"] = existing.event_id
                outcome["revision"] = existing.current_revision
        return outcome

    # Persist evidence first — even unparseable payloads are newsroom
    # evidence (§9). Normalization happens after the raw row exists.
    obs_record = RawObservation(
        observation_id=repo.new_id("obs"), source=adapter.name,
        source_event_id=None, feed_url=feed_url, retrieved_at=retrieved_at,
        source_updated_at=None, payload_hash=obs_hash, raw_payload=raw_payload,
    )
    # One observation == one atomic state transition (§75): evidence row,
    # correlation, revision, facts, and scoring commit together or not at all.
    from .storage.database import tx
    with tx(conn):
        try:
            norm = adapter.normalize(raw_payload)
        except Exception:  # noqa: BLE001 — malformed payload must not kill a poll
            norm = None
        if norm:
            obs_record.source_event_id = norm.source_event_id
            obs_record.source_updated_at = norm.source_updated_at
        if not repo.insert_observation(conn, obs_record):
            outcome["skipped"] = "duplicate payload"
            return outcome
        outcome["new_observation"] = 1
        if norm is None:
            outcome["skipped"] = "unparseable payload"
            log_event(log, 30, "unparseable payload stored as evidence",
                      source=adapter.name, hash=obs_hash[:12])
            return outcome

        event, reason = _correlate(conn, norm, load_settings())
        snapshot = canonical_fields(norm)

        if event is None:
            event = CanonicalEvent(
                event_id=canonical_event_id(norm.event_type, norm.source_event_id),
                event_type=norm.event_type,
                status=norm.status or EventStatus.ACTIVE.value,
                severity=norm.severity or 0.0, confidence=0.0,
                first_seen=retrieved_at, last_updated=retrieved_at,
                latitude=norm.latitude, longitude=norm.longitude,
                location_label=norm.location_label, title=norm.title,
                occurred_at=norm.occurred_at,
            )
            outcome["new_event"] = 1
        else:
            outcome["merged"] = 1 if norm.source not in {
                s.split(":")[0] for s in event.source_ids
            } else 0

        repo.add_event_source(conn, event.event_id, norm.source, norm.source_event_id)
        repo.link_observation(conn, obs_record.observation_id, event.event_id)

        prior = repo.latest_revision_snapshot(conn, event.event_id)
        changed = diff_snapshots(prior, snapshot)
        if prior is None or changed:
            revision_no = event.current_revision + 1
            repo.insert_revision(conn, EventRevision(
                event_id=event.event_id, revision=revision_no,
                created_at=retrieved_at, snapshot=snapshot,
                changed_fields=changed, observation_id=obs_record.observation_id,
            ))
            event.current_revision = revision_no
            outcome["new_revision"] = 1

            ledger = FactLedger(conn)
            facts = ledger.apply_snapshot(
                event.event_id, revision_no, snapshot, norm.source,
                obs_record.observation_id, confidence=1.0,
            )
            outcome["facts_written"] = len(facts)
            outcome["changed_fields"] = changed
        else:
            outcome["skipped"] = "no material change"

        # Rescore on every observation (cheap, deterministic).
        merged_snapshot = (prior | snapshot) if prior else snapshot
        event.severity = merged_snapshot.get("severity") or event.severity
        event.status = norm.status or event.status
        event.latitude = norm.latitude or event.latitude
        event.longitude = norm.longitude or event.longitude
        event.location_label = norm.location_label or event.location_label
        event.title = norm.title or event.title
        event.last_updated = retrieved_at
        event.occurred_at = norm.occurred_at or event.occurred_at
        event.extra.update({"source_status": norm.status})
        # Fusion discriminator for sub-typed families (§16): adapters expose
        # it as fields.alert_event; stored on the event so candidate checks
        # stay comparable across sources.
        if norm.fields.get("alert_event"):
            event.extra["alert_event"] = norm.fields["alert_event"]
        if norm.fields.get("series_id"):
            event.extra["series_id"] = norm.fields["series_id"]

        impact_score, level = score_impact(event.event_type, merged_snapshot,
                                           load_settings())
        event.impact_score, event.impact_level = impact_score, level
        event.confidence_score = score_confidence(
            norm.source, norm.reviewed,
            corroborating_source_count(conn, event.event_id),
            load_settings(),
        )
        event.confidence = event.confidence_score
        repo.upsert_event(conn, event)

        # Auto-render gate (§72): revisions scoring high enough queue a
        # SHORT_RENDER job inside the same transaction — a scored revision
        # never exists without its render job. Deduped per (event, revision).
        settings = load_settings()
        auto = settings.get("auto_render", {}) or {}
        levels = ["BACKGROUND", "NORMAL", "UPDATE", "BREAKING", "FLASH"]
        if (levels.index(level) >= levels.index(auto.get("min_impact", "UPDATE"))
                and event.confidence_score >= float(
                    auto.get("min_confidence", 0.70))):
            dup = conn.execute(
                "SELECT 1 FROM jobs WHERE event_id=? AND revision=?"
                " AND job_type=? AND status IN ('queued','running','done')"
                " LIMIT 1",
                (event.event_id, event.current_revision, JobType.SHORT_RENDER),
            ).fetchone()
            if not dup:
                outcome["jobs"] = enqueue_story_pipeline(
                    conn, event.event_id, event.current_revision, level)

    outcome["event_id"] = event.event_id
    outcome["revision"] = event.current_revision
    outcome["impact"] = {"score": impact_score, "level": level}
    outcome["confidence"] = event.confidence_score
    outcome["gate"] = production_gate(event.confidence_score, load_settings())
    outcome["correlation"] = reason

    log_event(log, 20, "ingested",
              event_id=event.event_id, revision=event.current_revision,
              impact=f"{impact_score}/{level}", confidence=event.confidence_score,
              changed=changed or "none", reason=reason)
    return outcome


# ------------------------------------------------------------------- story jobs

def enqueue_story_pipeline(
    conn: sqlite3.Connection, event_id: str, revision: int, level: str
) -> list[str]:
    """Queue the media pipeline for an event revision (§71, §72)."""
    priority = {
        "FLASH": Priority.FLASH, "BREAKING": Priority.BREAKING,
        "UPDATE": Priority.SHORT, "NORMAL": Priority.NORMAL,
    }.get(level, Priority.LONGFORM)
    job_ids = []
    for job_type in (JobType.SHORT_RENDER,):
        job = Job(
            job_id=repo.new_id("job"), job_type=job_type,
            status=JobStatus.QUEUED, priority=int(priority),
            event_id=event_id, revision=revision, payload_json=None,
            created_at=repo.utcnow(),
        )
        repo.enqueue_job(conn, job)
        job_ids.append(job.job_id)
    return job_ids
