"""Keep the render queue useful without deleting its audit history."""
from conftest import fixture

from newsroom.models.event import CanonicalEvent
from newsroom.models.job import Job
from newsroom.pipeline import ingest_observation
from newsroom.service import ServiceLoop
from newsroom.storage import repositories as repo


def _event(conn, name, kind="news_report", level="UPDATE", revision=1):
    repo.upsert_event(conn, CanonicalEvent(
        event_id=name, event_type=kind, status="active", severity=0.5,
        confidence=0.9, first_seen=repo.utcnow(), last_updated=repo.utcnow(),
        impact_level=level, confidence_score=0.9, current_revision=revision))


def _job(conn, name, event_id, revision=1, priority=30, created="2026-11-09T10:00:00Z"):
    repo.enqueue_job(conn, Job(
        job_id=name, job_type="SHORT_RENDER", status="queued",
        priority=priority, event_id=event_id, revision=revision,
        payload_json=None, created_at=created))


def test_cancel_obsolete_queue_keeps_records_and_urgent_weather(conn):
    _event(conn, "current")
    _event(conn, "revised", revision=2)
    _event(conn, "restricted")
    repo.add_event_source(conn, "restricted", "gnews_crime", "old")
    _event(conn, "ticker", kind="weather_alert", level="UPDATE")
    _event(conn, "urgent", kind="weather_alert", level="FLASH")
    _job(conn, "current", "current")
    _job(conn, "old", "revised")
    _job(conn, "restricted", "restricted")
    _job(conn, "ticker", "ticker")
    _job(conn, "urgent", "urgent")
    _job(conn, "missing", "removed")
    conn.execute("UPDATE jobs SET status='running' WHERE job_id='current'")

    assert repo.cancel_unairable_render_jobs(conn) == 4
    assert repo.cancel_unairable_render_jobs(conn) == 0
    rows = {r["job_id"]: r for r in conn.execute("SELECT * FROM jobs")}
    assert len(rows) == 6
    assert rows["old"]["error"] == "superseded revision"
    assert rows["restricted"]["error"] == "restricted source"
    assert rows["ticker"]["error"] == "weather ticker only"
    assert rows["missing"]["error"] == "event missing"
    assert all(rows[name]["status"] == "cancelled" and rows[name]["completed_at"]
               for name in ("old", "restricted", "ticker", "missing"))
    assert rows["urgent"]["status"] == "queued"
    assert rows["current"]["status"] == "running"


def test_same_priority_renders_newest_first_without_overriding_urgent(conn):
    for name in ("old", "new", "urgent"):
        _event(conn, name)
    _job(conn, "old", "old", created="2026-11-09T10:00:00Z")
    _job(conn, "new", "new", created="2026-11-09T11:00:00Z")
    _job(conn, "urgent", "urgent", priority=0,
         created="2026-11-09T09:00:00Z")
    assert [repo.claim_next_job(conn).job_id for _ in range(3)] == [
        "urgent", "new", "old"]


def test_core_skips_obsolete_jobs_before_render(conn, settings, monkeypatch):
    _event(conn, "revised", revision=2)
    _event(conn, "fresh")
    _job(conn, "old", "revised")
    _job(conn, "fresh", "fresh")
    rendered = []
    loop = ServiceLoop(conn, settings)
    monkeypatch.setattr(loop, "_run_job",
                        lambda job_type, event_id: rendered.append(event_id))
    assert loop.drain_jobs(max_jobs=1) == 1
    assert rendered == ["fresh"]
    assert conn.execute(
        "SELECT status FROM jobs WHERE job_id='old'").fetchone()[0] == "cancelled"


def test_ingestion_queues_urgent_but_not_ticker_weather(conn, nws, monkeypatch):
    import newsroom.pipeline as pipeline

    original_score = pipeline.score_impact
    monkeypatch.setattr(pipeline, "score_impact",
                        lambda kind, snap, settings: (0.35, "UPDATE"))
    out = ingest_observation(conn, nws, fixture("nws_warning.json"),
                             "fixture://nws", "2026-11-07T15:05:00Z")
    assert out["new_revision"] == 1
    assert not out.get("jobs")
    assert repo.queue_depth(conn).get("queued", 0) == 0

    monkeypatch.setattr(pipeline, "score_impact", original_score)
    out = ingest_observation(conn, nws, fixture("nws_warning_rev2.json"),
                             "fixture://nws", "2026-11-07T16:35:00Z")
    assert out["new_revision"] == 1
    assert len(out["jobs"]) == 1
    assert repo.queue_depth(conn)["queued"] == 1
