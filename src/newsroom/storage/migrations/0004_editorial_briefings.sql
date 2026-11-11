CREATE TABLE source_rights_decisions (
    source TEXT PRIMARY KEY,
    ingest_allowed INTEGER NOT NULL DEFAULT 0,
    summary_allowed INTEGER NOT NULL DEFAULT 0,
    quote_limit INTEGER NOT NULL DEFAULT 0,
    images_video_allowed INTEGER NOT NULL DEFAULT 0,
    commercial_publication_allowed INTEGER NOT NULL DEFAULT 0,
    attribution_required INTEGER NOT NULL DEFAULT 1,
    terms_url TEXT NOT NULL,
    reviewed_by TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE TABLE briefing_drafts (
    draft_id TEXT PRIMARY KEY,
    tier TEXT NOT NULL CHECK (tier IN ('FREE','PRO')),
    profile_json TEXT NOT NULL,
    status TEXT NOT NULL,
    current_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE briefing_revisions (
    draft_id TEXT NOT NULL REFERENCES briefing_drafts(draft_id),
    revision_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    html TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    event_refs_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (draft_id, revision_id)
);
CREATE TABLE editorial_review_actions (
    action_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL,
    revision_id INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT,
    destinations_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (draft_id, revision_id) REFERENCES briefing_revisions(draft_id, revision_id)
);
CREATE TABLE editorial_outbox (
    idempotency_key TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL,
    revision_id INTEGER NOT NULL,
    recipient TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind='review'),
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    in_reply_to TEXT,
    provider_receipt TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (draft_id, revision_id) REFERENCES briefing_revisions(draft_id, revision_id),
    UNIQUE (draft_id, revision_id, recipient, kind)
);
CREATE INDEX idx_editorial_outbox_ready ON editorial_outbox(status, next_attempt_at);
CREATE TRIGGER briefing_revision_no_update BEFORE UPDATE ON briefing_revisions
BEGIN SELECT RAISE(ABORT, 'briefing revisions are immutable'); END;
CREATE TRIGGER briefing_revision_no_delete BEFORE DELETE ON briefing_revisions
BEGIN SELECT RAISE(ABORT, 'briefing revisions are immutable'); END;
CREATE TRIGGER editorial_action_no_update BEFORE UPDATE ON editorial_review_actions
BEGIN SELECT RAISE(ABORT, 'review actions are immutable'); END;
CREATE TRIGGER editorial_action_no_delete BEFORE DELETE ON editorial_review_actions
BEGIN SELECT RAISE(ABORT, 'review actions are immutable'); END;
