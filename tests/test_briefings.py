import json
import sqlite3
from datetime import datetime, timezone

import pytest

from conftest import fixture
from newsroom.ingestion.adapters.usgs import UsgsAdapter
from newsroom.pipeline import ingest_observation
from newsroom.storage import repositories as repo


def seeded_event(conn, name="earthquake_major.geojson", event_id="illinois1",
                 title="Earthquake in Central Illinois"):
    payload = json.loads(fixture(name))
    now = datetime.now(timezone.utc)
    payload["id"] = event_id
    payload["properties"]["code"] = event_id
    payload["properties"]["place"] = "Central Illinois, United States"
    payload["properties"]["title"] = title
    payload["properties"]["time"] = int(now.timestamp() * 1000)
    payload["properties"]["updated"] = int(now.timestamp() * 1000)
    payload["properties"]["url"] = f"https://earthquake.usgs.gov/earthquakes/eventpage/{event_id}"
    payload["geometry"]["coordinates"] = [-89.4, 40.0, 10.0]
    outcome = ingest_observation(
        conn, UsgsAdapter(name="usgs", config={}), json.dumps(payload),
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
        repo.utcnow(),
    )
    return outcome["event_id"]


def test_briefing_review_revision_and_mock_delivery(conn, settings):
    from newsroom.briefings import compose, record_rights, revise, reconcile
    from newsroom.editorial import approve, request_changes, inspect
    from newsroom.delivery import FakeMailer, drain_review, mock_dispatch

    event_id = seeded_event(conn)
    assert compose(conn, settings, "FREE") is None
    record_rights(conn, "usgs", "fixture-reviewer", "https://earthquake.usgs.gov/")
    free = compose(conn, settings, "FREE")
    pro = compose(conn, settings, "PRO", {"state": "IL", "topics": ["earthquake"]})
    assert free and pro and free != pro
    assert compose(conn, settings, "FREE") == free
    assert compose(conn, settings, "PRO", {"state": "NY", "topics": ["earthquake"]}) is None
    assert len(inspect(conn, free)["revision"]["evidence"]) > 0
    assert "PRO" not in inspect(conn, free)["revision"]["text"]
    assert conn.execute("SELECT COUNT(*) FROM editorial_outbox").fetchone()[0] == 2
    mailer = FakeMailer()
    assert drain_review(conn, mailer) == 2
    assert drain_review(conn, mailer) == 0
    assert len(mailer.messages) == 2
    assert all("NOT PUBLISHED" in m["body"] for m in mailer.messages)
    assert all(m["to"] == "jarvintels@gmail.com" for m in mailer.messages)
    request_changes(conn, free, "Check county attribution", "local-owner")
    updated = revise(conn, settings, free, "County attribution under review.")
    assert updated == 2
    assert drain_review(conn, mailer) == 1
    assert mailer.messages[-1]["in_reply_to"] == mailer.messages[0]["message_id"]
    assert len(inspect(conn, free)["history"]) == 2
    digest = inspect(conn, free)["revision"]["content_sha256"]
    approve(conn, free, 2, digest, ["mock"], "local-owner")
    assert mock_dispatch(conn, settings, free, 2) == "disabled"
    seeded_event(conn, "earthquake_rev2.geojson", "illinois1")
    assert reconcile(conn) >= 1
    assert mock_dispatch(conn, settings, free, 2) == "stale"


def test_review_retry_and_approval_guards(conn, settings):
    from newsroom.briefings import compose, record_rights
    from newsroom.editorial import approve, inspect
    from newsroom.delivery import FakeMailer, drain_review, mock_dispatch

    seeded_event(conn)
    record_rights(conn, "usgs", "fixture-reviewer", "https://earthquake.usgs.gov/")
    draft = compose(conn, settings, "FREE")
    mailer = FakeMailer(fail_once=True)
    assert drain_review(conn, mailer) == 0
    assert conn.execute("SELECT attempts FROM editorial_outbox").fetchone()[0] == 1
    assert drain_review(conn, mailer) == 0
    conn.execute("UPDATE editorial_outbox SET next_attempt_at=?", ("2000-01-01T00:00:00Z",))
    assert drain_review(conn, mailer) == 1
    rev = inspect(conn, draft)["revision"]
    with pytest.raises(ValueError):
        approve(conn, draft, 1, "bad-hash", ["mock"], "local-owner")
    with pytest.raises(ValueError):
        approve(conn, draft, 1, rev["content_sha256"], ["public"], "local-owner")
    assert mock_dispatch(conn, settings, draft, 1) == "unapproved"
    approve(conn, draft, 1, rev["content_sha256"], ["mock"], "local-owner")
    assert mock_dispatch(conn, settings, draft, 1) == "disabled"


def test_immutable_revisions_rights_and_html(conn, settings):
    from newsroom.briefings import compose, record_rights, reconcile
    from newsroom.editorial import approve, inspect

    seeded_event(conn, title="<img src=x onerror=alert(1)> Earthquake in Central Illinois")
    with pytest.raises(ValueError):
        record_rights(conn, "usgs", "reviewer", "https://127.0.0.1/")
    record_rights(conn, "usgs", "reviewer", "https://earthquake.usgs.gov/")
    draft = compose(conn, settings, "FREE")
    revision = inspect(conn, draft)["revision"]
    assert "<img" not in revision["html"]
    assert "&lt;img" in revision["html"]
    assert "https://earthquake.usgs.gov/earthquakes/eventpage/illinois1" in revision["text"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE briefing_revisions SET text='altered' WHERE draft_id=?", (draft,))
    approve(conn, draft, 1, revision["content_sha256"], ["mock"], "reviewer")
    conn.execute("UPDATE source_rights_decisions SET commercial_publication_allowed=0 WHERE source='usgs'")
    assert reconcile(conn) == 1
    assert inspect(conn, draft)["draft"]["status"] == "EXPIRED"
    assert compose(conn, settings, "PRO", {"state": "IL", "topics": ["earthquake"]}) is None


def test_migration_from_prior_schema(tmp_path):
    from newsroom.storage.database import connect, migrate, MIGRATIONS_DIR

    db = connect(tmp_path / "old.db")
    for number in (1, 2, 3):
        sql = next(MIGRATIONS_DIR.glob(f"000{number}_*.sql"))
        db.executescript(sql.read_text())
        db.execute("INSERT INTO schema_migrations VALUES (?, ?)", (number, repo.utcnow()))
    db.execute("INSERT INTO events (event_id, event_type, status, first_seen, last_updated) "
               "VALUES ('old', 'earthquake', 'active', '2026-01-01', '2026-01-01')")
    migrate(db)
    assert db.execute("SELECT event_id FROM events").fetchone()[0] == "old"
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrate(db) >= 4
    db.close()
