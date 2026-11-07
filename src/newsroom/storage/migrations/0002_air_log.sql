-- 0002: air log — what the Broadcast Director put on air (§31-§35).
-- Append-only audit trail for rundown scheduling and post-hoc review.

CREATE TABLE IF NOT EXISTS air_log (
    air_id      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,           -- 'story' | 'standby' | 'sting'
    event_id    TEXT,
    revision    INTEGER,
    rundown_id  TEXT,                    -- broadcast.yaml segment id
    asset_path  TEXT,
    started_at  TEXT NOT NULL,
    duration_s  REAL,
    interrupted INTEGER NOT NULL DEFAULT 0,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_air_log_started ON air_log(started_at);
CREATE INDEX IF NOT EXISTS idx_air_log_rundown ON air_log(rundown_id, started_at);
