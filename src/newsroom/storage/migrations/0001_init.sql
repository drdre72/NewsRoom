-- Newsroom schema v1. Append-only evidence; SQLite is the state owner (§2, §9).

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- Raw source evidence (§9). raw_payload is never updated or deleted.
CREATE TABLE observations (
    observation_id      TEXT PRIMARY KEY,
    source              TEXT NOT NULL,
    source_event_id     TEXT,
    feed_url            TEXT,
    retrieved_at        TEXT NOT NULL,
    source_updated_at   TEXT,
    payload_hash        TEXT NOT NULL,
    raw_payload         TEXT NOT NULL,
    event_id            TEXT
);
CREATE INDEX idx_obs_source_event ON observations(source, source_event_id);
CREATE UNIQUE INDEX idx_obs_dedup ON observations(source, source_event_id, payload_hash);

-- Canonical events (§8).
CREATE TABLE events (
    event_id            TEXT PRIMARY KEY,
    event_type          TEXT NOT NULL,
    status              TEXT NOT NULL,
    severity            REAL,
    confidence          REAL,
    impact_score        REAL,
    impact_level        TEXT,
    confidence_score    REAL,
    first_seen          TEXT NOT NULL,
    last_updated        TEXT NOT NULL,
    latitude            REAL,
    longitude           REAL,
    location_label      TEXT,
    occurred_at         TEXT,
    title               TEXT,
    current_revision    INTEGER NOT NULL DEFAULT 0,
    extra_json          TEXT
);
CREATE INDEX idx_events_type_status ON events(event_type, status);

-- Which source IDs were merged into a canonical event (§16).
CREATE TABLE event_sources (
    event_id        TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    first_seen      TEXT NOT NULL,
    PRIMARY KEY (event_id, source, source_event_id)
);
CREATE INDEX idx_event_sources_lookup ON event_sources(source, source_event_id);

-- Material-change history (§8, §17).
CREATE TABLE event_revisions (
    event_id            TEXT NOT NULL,
    revision            INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    snapshot_json       TEXT NOT NULL,
    changed_fields_json TEXT NOT NULL,
    observation_id      TEXT,
    PRIMARY KEY (event_id, revision)
);

-- Fact ledger (§10). status: current | superseded | disputed.
CREATE TABLE facts (
    fact_id         TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    field           TEXT NOT NULL,
    value_json      TEXT NOT NULL,
    value_display   TEXT,
    source          TEXT,
    observation_id  TEXT,
    confidence      REAL NOT NULL DEFAULT 1.0,
    status          TEXT NOT NULL DEFAULT 'current',
    superseded_by   TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_facts_event ON facts(event_id, field, status);

-- Job queue (§71).
CREATE TABLE jobs (
    job_id          TEXT PRIMARY KEY,
    job_type        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    priority        INTEGER NOT NULL DEFAULT 50,
    event_id        TEXT,
    revision        INTEGER,
    payload_json    TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    completed_at    TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);
CREATE INDEX idx_jobs_status_priority ON jobs(status, priority, created_at);

-- Stories / scripts with provenance.
CREATE TABLE stories (
    story_id        TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    plan_json       TEXT,
    script_json     TEXT,
    generator       TEXT,
    validated       INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- Generated assets (audio, shots, clips, video, manifests) — §44, §45.
CREATE TABLE assets (
    asset_id        TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    path            TEXT NOT NULL,
    sha256          TEXT,
    event_id        TEXT,
    revision        INTEGER,
    meta_json       TEXT,
    created_at      TEXT NOT NULL
);

-- Publication gate / manifests (§45, §47).
CREATE TABLE publications (
    publication_id  TEXT PRIMARY KEY,
    story_id        TEXT,
    event_id        TEXT,
    platform        TEXT,
    status          TEXT NOT NULL,
    manifest_json   TEXT,
    created_at      TEXT NOT NULL
);
