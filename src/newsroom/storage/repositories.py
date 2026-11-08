"""Repositories — the only code that touches SQL beyond migrations.

All state reads/writes go through here so the truth boundary stays auditable:
sources establish facts, code (this layer) establishes state.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from ..models.event import CanonicalEvent, EventRevision
from ..models.fact import Fact
from ..models.job import Job
from ..models.observation import RawObservation


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def payload_hash(payload: str | bytes) -> str:
    data = payload.encode() if isinstance(payload, str) else payload
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- observations

def insert_observation(conn: sqlite3.Connection, obs: RawObservation) -> bool:
    """Insert raw evidence. Returns False if the exact payload already exists."""
    from .database import tx
    try:
        with tx(conn):  # standalone: commits; inside outer tx(): defers
            conn.execute(
                "INSERT INTO observations (observation_id, source, source_event_id,"
                " feed_url, retrieved_at, source_updated_at, payload_hash,"
                " raw_payload, event_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (obs.observation_id, obs.source, obs.source_event_id, obs.feed_url,
                 obs.retrieved_at, obs.source_updated_at, obs.payload_hash,
                 obs.raw_payload, obs.event_id),
            )
        return True
    except sqlite3.IntegrityError:
        return False  # same source_event_id + payload_hash already stored


def observation_hash_seen(conn: sqlite3.Connection, payload_hash: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM observations WHERE payload_hash=? LIMIT 1", (payload_hash,)
    ).fetchone()
    return row is not None


def link_observation(conn: sqlite3.Connection, observation_id: str, event_id: str) -> None:
    conn.execute(
        "UPDATE observations SET event_id=? WHERE observation_id=?",
        (event_id, observation_id),
    )


def get_observation(conn: sqlite3.Connection, observation_id: str) -> RawObservation | None:
    row = conn.execute(
        "SELECT * FROM observations WHERE observation_id=?", (observation_id,)
    ).fetchone()
    return _row_to_obs(row) if row else None


def latest_observation_for_source_event(
    conn: sqlite3.Connection, source: str, source_event_id: str
) -> RawObservation | None:
    row = conn.execute(
        "SELECT * FROM observations WHERE source=? AND source_event_id=?"
        " ORDER BY retrieved_at DESC LIMIT 1",
        (source, source_event_id),
    ).fetchone()
    return _row_to_obs(row) if row else None


def _row_to_obs(row: sqlite3.Row) -> RawObservation:
    return RawObservation(
        observation_id=row["observation_id"], source=row["source"],
        source_event_id=row["source_event_id"], feed_url=row["feed_url"],
        retrieved_at=row["retrieved_at"], source_updated_at=row["source_updated_at"],
        payload_hash=row["payload_hash"], raw_payload=row["raw_payload"],
        event_id=row["event_id"],
    )


# --------------------------------------------------------------------- events

def upsert_event(conn: sqlite3.Connection, ev: CanonicalEvent) -> None:
    conn.execute(
        "INSERT INTO events (event_id, event_type, status, severity, confidence,"
        " impact_score, impact_level, confidence_score, first_seen, last_updated,"
        " latitude, longitude, location_label, occurred_at, title,"
        " current_revision, extra_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(event_id) DO UPDATE SET status=excluded.status,"
        " severity=excluded.severity, confidence=excluded.confidence,"
        " impact_score=excluded.impact_score, impact_level=excluded.impact_level,"
        " confidence_score=excluded.confidence_score, last_updated=excluded.last_updated,"
        " latitude=excluded.latitude, longitude=excluded.longitude,"
        " location_label=excluded.location_label, occurred_at=excluded.occurred_at,"
        " title=excluded.title,"
        " current_revision=excluded.current_revision, extra_json=excluded.extra_json",
        (ev.event_id, ev.event_type, ev.status, ev.severity, ev.confidence,
         ev.impact_score, ev.impact_level, ev.confidence_score, ev.first_seen,
         ev.last_updated, ev.latitude, ev.longitude, ev.location_label,
         ev.occurred_at, ev.title,
         ev.current_revision, json.dumps(ev.extra)),
    )


def get_event(conn: sqlite3.Connection, event_id: str) -> CanonicalEvent | None:
    row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
    return _row_to_event(conn, row) if row else None


def list_events(
    conn: sqlite3.Connection,
    event_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[CanonicalEvent]:
    q, params = "SELECT * FROM events", []
    clauses = []
    if event_type:
        clauses.append("event_type=?"); params.append(event_type)
    if status:
        clauses.append("status=?"); params.append(status)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY last_updated DESC LIMIT ?"; params.append(limit)
    return [_row_to_event(conn, r) for r in conn.execute(q, params)]


def _row_to_event(conn: sqlite3.Connection, row: sqlite3.Row) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=row["event_id"], event_type=row["event_type"], status=row["status"],
        severity=row["severity"] or 0.0, confidence=row["confidence"] or 0.0,
        first_seen=row["first_seen"], last_updated=row["last_updated"],
        latitude=row["latitude"], longitude=row["longitude"],
        location_label=row["location_label"], occurred_at=row["occurred_at"],
        title=row["title"],
        impact_score=row["impact_score"] or 0.0,
        impact_level=row["impact_level"] or "BACKGROUND",
        confidence_score=row["confidence_score"] or 0.0,
        current_revision=row["current_revision"],
        source_ids=get_event_source_ids(conn, row["event_id"]),
        extra=json.loads(row["extra_json"] or "{}"),
    )


def get_event_by_source_id(
    conn: sqlite3.Connection, source: str, source_event_id: str
) -> CanonicalEvent | None:
    row = conn.execute(
        "SELECT event_id FROM event_sources WHERE source=? AND source_event_id=?",
        (source, source_event_id),
    ).fetchone()
    return get_event(conn, row["event_id"]) if row else None


def add_event_source(
    conn: sqlite3.Connection, event_id: str, source: str, source_event_id: str
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO event_sources (event_id, source, source_event_id, first_seen)"
        " VALUES (?,?,?,?)", (event_id, source, source_event_id, utcnow()),
    )


def get_event_source_ids(conn: sqlite3.Connection, event_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT source, source_event_id FROM event_sources WHERE event_id=?", (event_id,)
    ).fetchall()
    return [f"{r['source']}:{r['source_event_id']}" for r in rows]


def candidate_events_for_fusion(
    conn: sqlite3.Connection, event_type: str, since_iso: str
) -> list[CanonicalEvent]:
    rows = conn.execute(
        "SELECT * FROM events WHERE event_type=? AND (occurred_at>=? OR occurred_at IS NULL)",
        (event_type, since_iso),
    ).fetchall()
    return [_row_to_event(conn, r) for r in rows]


# ------------------------------------------------------------------- revisions

def insert_revision(conn: sqlite3.Connection, rev: EventRevision) -> None:
    conn.execute(
        "INSERT INTO event_revisions (event_id, revision, created_at, snapshot_json,"
        " changed_fields_json, observation_id) VALUES (?,?,?,?,?,?)",
        (rev.event_id, rev.revision, rev.created_at, json.dumps(rev.snapshot),
         json.dumps(rev.changed_fields), rev.observation_id),
    )


def get_revisions(conn: sqlite3.Connection, event_id: str) -> list[EventRevision]:
    rows = conn.execute(
        "SELECT * FROM event_revisions WHERE event_id=? ORDER BY revision", (event_id,)
    ).fetchall()
    return [
        EventRevision(
            event_id=r["event_id"], revision=r["revision"], created_at=r["created_at"],
            snapshot=json.loads(r["snapshot_json"]),
            changed_fields=json.loads(r["changed_fields_json"]),
            observation_id=r["observation_id"],
        )
        for r in rows
    ]


def latest_revision_snapshot(conn: sqlite3.Connection, event_id: str) -> dict | None:
    row = conn.execute(
        "SELECT snapshot_json FROM event_revisions WHERE event_id=?"
        " ORDER BY revision DESC LIMIT 1", (event_id,),
    ).fetchone()
    return json.loads(row["snapshot_json"]) if row else None


# ----------------------------------------------------------------------- facts

def insert_fact(conn: sqlite3.Connection, fact: Fact) -> None:
    conn.execute(
        "INSERT INTO facts (fact_id, event_id, revision, field, value_json,"
        " value_display, source, observation_id, confidence, status,"
        " superseded_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (fact.fact_id, fact.event_id, fact.revision, fact.field,
         json.dumps(fact.value), fact.value_display, fact.source,
         fact.observation_id, fact.confidence, fact.status,
         fact.superseded_by, fact.created_at),
    )


def supersede_fact(conn: sqlite3.Connection, fact_id: str, superseded_by: str) -> None:
    conn.execute(
        "UPDATE facts SET status='superseded', superseded_by=? WHERE fact_id=?",
        (superseded_by, fact_id),
    )


def current_facts(conn: sqlite3.Connection, event_id: str) -> list[Fact]:
    return _facts(conn, "SELECT * FROM facts WHERE event_id=? AND status='current'"
                  " ORDER BY field", (event_id,))


def all_facts(conn: sqlite3.Connection, event_id: str) -> list[Fact]:
    return _facts(conn, "SELECT * FROM facts WHERE event_id=?"
                  " ORDER BY field, revision", (event_id,))


def facts_for_field(conn: sqlite3.Connection, event_id: str, field: str) -> list[Fact]:
    return _facts(conn, "SELECT * FROM facts WHERE event_id=? AND field=?"
                  " ORDER BY revision", (event_id, field))


def _facts(conn: sqlite3.Connection, query: str, params: tuple) -> list[Fact]:
    return [
        Fact(
            fact_id=r["fact_id"], event_id=r["event_id"], revision=r["revision"],
            field=r["field"], value=json.loads(r["value_json"]),
            value_display=r["value_display"], source=r["source"],
            observation_id=r["observation_id"], confidence=r["confidence"],
            status=r["status"], created_at=r["created_at"],
            superseded_by=r["superseded_by"],
        )
        for r in conn.execute(query, params)
    ]


# ------------------------------------------------------------------------ jobs

def enqueue_job(conn: sqlite3.Connection, job: Job) -> None:
    conn.execute(
        "INSERT INTO jobs (job_id, job_type, status, priority, event_id, revision,"
        " payload_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (job.job_id, job.job_type, job.status, job.priority, job.event_id,
         job.revision, job.payload_json, job.created_at),
    )


def claim_next_job(conn: sqlite3.Connection, job_types: list[str] | None = None) -> Job | None:
    q = "SELECT * FROM jobs WHERE status='queued'"
    params: list[Any] = []
    if job_types:
        q += f" AND job_type IN ({','.join('?' * len(job_types))})"
        params.extend(job_types)
    q += " ORDER BY priority, created_at LIMIT 1"
    row = conn.execute(q, params).fetchone()
    if not row:
        return None
    conn.execute(
        "UPDATE jobs SET status='running', started_at=? WHERE job_id=?",
        (utcnow(), row["job_id"]),
    )
    return _row_to_job(row)


def complete_job(conn: sqlite3.Connection, job_id: str, error: str | None = None) -> None:
    status = "failed" if error else "done"
    conn.execute(
        "UPDATE jobs SET status=?, completed_at=?, error=? WHERE job_id=?",
        (status, utcnow(), error, job_id),
    )


def retry_job(conn: sqlite3.Connection, job_id: str, error: str,
              max_retries: int = 3) -> None:
    """Requeue a failed job, or dead-letter it once retries are exhausted —
    a deterministic failure (e.g. validator rejection) must not loop forever."""
    conn.execute(
        "UPDATE jobs SET status=CASE WHEN retry_count+1>=? THEN 'failed' ELSE 'queued' END,"
        " started_at=NULL, completed_at=CASE WHEN retry_count+1>=? THEN ? ELSE NULL END,"
        " retry_count=retry_count+1, error=? WHERE job_id=?",
        (max_retries, max_retries, utcnow(), error, job_id),
    )


def queue_depth(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM jobs GROUP BY status"
    ).fetchall()
    return {r["status"]: r["c"] for r in rows}


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        job_id=row["job_id"], job_type=row["job_type"], status=row["status"],
        priority=row["priority"], event_id=row["event_id"], revision=row["revision"],
        payload_json=row["payload_json"], created_at=row["created_at"],
        started_at=row["started_at"], completed_at=row["completed_at"],
        retry_count=row["retry_count"], error=row["error"],
    )


# ---------------------------------------------------------------- stories/assets

def insert_story(
    conn: sqlite3.Connection, story_id: str, event_id: str, revision: int,
    plan_json: str, script_json: str | None, generator: str | None,
    validated: bool, status: str,
) -> None:
    conn.execute(
        "INSERT INTO stories (story_id, event_id, revision, plan_json, script_json,"
        " generator, validated, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (story_id, event_id, revision, plan_json, script_json, generator,
         int(validated), status, utcnow()),
    )


def insert_asset(
    conn: sqlite3.Connection, asset_id: str, kind: str, path: str,
    sha256: str | None, event_id: str | None, revision: int | None,
    meta: dict | None,
) -> None:
    conn.execute(
        "INSERT INTO assets (asset_id, kind, path, sha256, event_id, revision,"
        " meta_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (asset_id, kind, path, sha256, event_id, revision,
         json.dumps(meta or {}), utcnow()),
    )


def assets_for_event(conn: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM assets WHERE event_id=? ORDER BY created_at", (event_id,)
    ).fetchall()


def latest_video_asset(conn: sqlite3.Connection, event_id: str,
                       revision: int) -> sqlite3.Row | None:
    # Long-form uses the same "video" kind but is not a broadcast Short.
    # Archived (dehydrated) assets are excluded: their files are gone and
    # they must leave air rotation until a restore re-renders them.
    # air_blocked rows failed an on-air encode — they stay out of every
    # picker; a successful re-render mints a fresh row that airs fine.
    return conn.execute(
        "SELECT * FROM assets WHERE event_id=? AND revision=? AND kind='video'"
        " AND COALESCE(json_extract(meta_json, '$.format'), 'short')='short'"
        " AND COALESCE(json_extract(meta_json, '$.archived'), 0)=0"
        " AND COALESCE(json_extract(meta_json, '$.air_blocked'), 0)=0"
        " ORDER BY created_at DESC LIMIT 1", (event_id, revision),
    ).fetchone()


def get_asset(conn: sqlite3.Connection, asset_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()


def video_assets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every video asset, archived or not — the retention scan surface."""
    return conn.execute(
        "SELECT * FROM assets WHERE kind='video' ORDER BY created_at"
    ).fetchall()


def patch_asset_meta(conn: sqlite3.Connection, asset_id: str,
                     fields: dict) -> None:
    """Merge keys into an asset's meta_json (archive/restore bookkeeping)."""
    conn.execute(
        "UPDATE assets SET meta_json=json_patch(meta_json, ?)"
        " WHERE asset_id=?", (json.dumps(fields), asset_id),
    )


def set_asset_sha(conn: sqlite3.Connection, asset_id: str,
                  sha256: str) -> None:
    """Correct a drifted recorded hash — the file on disk is the truth."""
    conn.execute("UPDATE assets SET sha256=? WHERE asset_id=?",
                 (sha256, asset_id))


def block_asset_path(conn: sqlite3.Connection, path: str,
                     reason: str) -> int:
    """Mark every asset row pointing at a path that failed on air —
    one corrupt/missing file must not wedge the whole channel."""
    cur = conn.execute(
        "UPDATE assets SET meta_json=json_patch(meta_json, ?)"
        " WHERE kind='video' AND path=?",
        (json.dumps({"air_blocked": 1, "blocked_reason": reason[:200],
                     "blocked_at": utcnow()}), path),
    )
    return cur.rowcount


def story_for_render(conn: sqlite3.Connection, event_id: str,
                     revision: int) -> sqlite3.Row | None:
    """The validated story a render used — the script a recipe replays."""
    rows = stories_for_render(conn, event_id, revision)
    return rows[0] if rows else None


def stories_for_render(conn: sqlite3.Connection, event_id: str,
                       revision: int) -> list[sqlite3.Row]:
    """All validated stories for a render — short and longform renders can
    share a revision, so callers pick by plan story_type."""
    return conn.execute(
        "SELECT * FROM stories WHERE event_id=? AND revision=?"
        " AND validated=1 ORDER BY created_at DESC",
        (event_id, revision),
    ).fetchall()


def air_stats(conn: sqlite3.Connection, event_id: str,
              revision: int) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(started_at) AS first_at,"
        " MAX(started_at) AS last_at FROM air_log"
        " WHERE event_id=? AND revision=?", (event_id, revision),
    ).fetchone()
    return {"count": row["n"], "first_at": row["first_at"],
            "last_at": row["last_at"]}


def latest_air(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Most recent air_log row — callers judge recency for on-air checks."""
    return conn.execute(
        "SELECT asset_path, started_at FROM air_log"
        " ORDER BY started_at DESC LIMIT 1"
    ).fetchone()


def pending_job_for(conn: sqlite3.Connection, event_id: str,
                    revision: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM jobs WHERE event_id=? AND revision=?"
        " AND status IN ('queued','running') LIMIT 1",
        (event_id, revision),
    ).fetchone() is not None


def pending_publication_for(conn: sqlite3.Connection,
                            event_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM publications WHERE event_id=?"
        " AND status IN ('pending','queued','uploading') LIMIT 1",
        (event_id,),
    ).fetchone() is not None


# ------------------------------------------------------------------ air log

def log_air(conn: sqlite3.Connection, air_id: str, kind: str,
            event_id: str | None, revision: int | None, rundown_id: str | None,
            asset_path: str | None, duration_s: float,
            interrupted: bool = False, reason: str | None = None) -> None:
    conn.execute(
        "INSERT INTO air_log (air_id, kind, event_id, revision, rundown_id,"
        " asset_path, started_at, duration_s, interrupted, reason)"
        " VALUES (?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'),?,?,?)",
        (air_id, kind, event_id, revision, rundown_id, asset_path,
         duration_s, int(interrupted), reason),
    )


def aired_recently(conn: sqlite3.Connection, rundown_id: str,
                   within_seconds: int) -> list[sqlite3.Row]:
    """Items aired under this rundown segment within the lookback window."""
    return conn.execute(
        "SELECT * FROM air_log WHERE rundown_id=? AND started_at >="
        " strftime('%Y-%m-%dT%H:%M:%fZ','now', ?)",
        (rundown_id, f"-{int(within_seconds)} seconds"),
    ).fetchall()


def air_log_tail(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM air_log ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()


# --------------------------------------------------------------------- recipes
# Rehydration recipes: everything needed to rebuild a retired video as
# "pure instruction" — the approved media-retention policy (§44, §52).

def insert_recipe(conn: sqlite3.Connection, recipe: dict) -> None:
    conn.execute(
        "INSERT INTO recipes (recipe_id, asset_id, event_id, revision, format,"
        " path, video_path, video_sha256, video_bytes, recipe_sha256, status,"
        " last_air_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (recipe["recipe_id"], recipe["asset_id"], recipe["event_id"],
         recipe["revision"], recipe["format"], recipe["path"],
         recipe["video_path"], recipe["video_sha256"],
         recipe["video_bytes"], recipe["recipe_sha256"], recipe["status"],
         recipe.get("last_air_at"), recipe["created_at"]),
    )


def get_recipe(conn: sqlite3.Connection,
               recipe_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM recipes WHERE recipe_id=?", (recipe_id,)).fetchone()


def recipe_for_asset(conn: sqlite3.Connection,
                     asset_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM recipes WHERE asset_id=?"
        " ORDER BY created_at DESC LIMIT 1", (asset_id,)).fetchone()


def recipes_for_event(conn: sqlite3.Connection, event_id: str,
                      revision: int | None = None) -> list[sqlite3.Row]:
    if revision is None:
        return conn.execute(
            "SELECT * FROM recipes WHERE event_id=? ORDER BY created_at",
            (event_id,)).fetchall()
    return conn.execute(
        "SELECT * FROM recipes WHERE event_id=? AND revision=?"
        " ORDER BY created_at", (event_id, revision)).fetchall()


def list_recipes(conn: sqlite3.Connection,
                 status: str | None = None) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            "SELECT * FROM recipes WHERE status=? ORDER BY created_at",
            (status,)).fetchall()
    return conn.execute(
        "SELECT * FROM recipes ORDER BY created_at").fetchall()


def set_recipe_status(conn: sqlite3.Connection, recipe_id: str,
                      status: str, **fields) -> None:
    """Status transition + timestamp columns (verified_at, dehydrated_at,
    restored_at, verify_report_json passed through as extra fields)."""
    allowed = {"verified_at", "dehydrated_at", "restored_at",
               "verify_report_json"}
    sets = ["status=?"] + [f"{k}=?" for k in fields if k in allowed]
    params = [status] + [fields[k] for k in fields if k in allowed]
    params.append(recipe_id)
    conn.execute(f"UPDATE recipes SET {', '.join(sets)}"
                 f" WHERE recipe_id=?", params)
