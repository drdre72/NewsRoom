-- 0003: rehydration recipes — media retired to "pure instruction" form.
-- A recipe row tracks one retired video asset: the JSON recipe file holds
-- the validated script, fact snapshot, visual plan, render settings, and
-- hashes needed to rebuild it with `python main.py restore <id>`.
-- Status lifecycle: written -> verified -> dehydrated -> (restored).

CREATE TABLE IF NOT EXISTS recipes (
    recipe_id           TEXT PRIMARY KEY,
    asset_id            TEXT NOT NULL,
    event_id            TEXT NOT NULL,
    revision            INTEGER NOT NULL,
    format              TEXT NOT NULL DEFAULT 'short',
    path                TEXT NOT NULL,           -- the recipe JSON file
    video_path          TEXT NOT NULL,           -- the retired MP4's path
    video_sha256        TEXT,
    video_bytes         INTEGER,
    recipe_sha256       TEXT NOT NULL,
    status              TEXT NOT NULL,           -- written|verified|dehydrated|verify_failed|restored
    verify_report_json  TEXT,
    last_air_at         TEXT,
    created_at          TEXT NOT NULL,
    verified_at         TEXT,
    dehydrated_at       TEXT,
    restored_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_recipes_event ON recipes(event_id, revision);
CREATE INDEX IF NOT EXISTS idx_recipes_asset ON recipes(asset_id);
CREATE INDEX IF NOT EXISTS idx_recipes_status ON recipes(status);
