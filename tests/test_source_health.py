"""A successful empty/unchanged feed is healthy; errors are not."""
from datetime import datetime, timedelta, timezone

from newsroom.config import Settings
from newsroom.intelligence.source_health import record_poll, source_statuses


def _settings(tmp_path):
    return Settings({
        "polling": {"interval_seconds": 60},
        "sources": {
            "no_storms": {"enabled": True, "poll_every_seconds": 900},
            "broken": {"enabled": True, "poll_every_seconds": 60},
            "waiting_for_key": {"enabled": True, "poll_every_seconds": 900},
        },
    }, tmp_path)


def test_empty_feed_is_healthy_and_credentials_are_visible(tmp_path):
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    path = tmp_path / "health.json"
    settings = _settings(tmp_path)
    record_poll(settings, {"sources": {
        "no_storms": {"ok": True, "items": 0},
        "broken": {"ok": False, "error": "HTTP Error 404"},
        "waiting_for_key": {"ok": True, "skipped": "credentials"},
    }}, path=path, now=now)
    assert path.stat().st_mode & 0o077 == 0
    statuses = {row["source"]: row for row in
                source_statuses(settings, path=path, now=now)}
    assert statuses["no_storms"]["status"] == "healthy"
    assert statuses["broken"]["status"] == "error"
    assert statuses["broken"]["error"] == "HTTP Error 404"
    assert statuses["waiting_for_key"]["status"] == "credentials"

    record_poll(settings, {"sources": {
        "no_storms": {"ok": True, "skipped": "interval"},
    }}, path=path, now=now + timedelta(minutes=1))
    statuses = {row["source"]: row for row in
                source_statuses(settings, path=path, now=now + timedelta(hours=1))}
    assert statuses["no_storms"]["status"] == "stale"


def test_recovery_clears_error_and_preserves_success_across_failures(tmp_path):
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    path = tmp_path / "health.json"
    settings = _settings(tmp_path)
    record_poll(settings, {"sources": {"broken": {"ok": True, "items": 1}}},
                path=path, now=now)
    record_poll(settings, {"sources": {
        "broken": {"ok": False, "error": "timeout"},
    }}, path=path, now=now + timedelta(minutes=1))
    status = {row["source"]: row for row in
              source_statuses(settings, path=path,
                              now=now + timedelta(minutes=1))}
    assert status["broken"]["status"] == "error"
    assert status["broken"]["last_success"] == now.isoformat()
    record_poll(settings, {"sources": {"broken": {"ok": True, "items": 0}}},
                path=path, now=now + timedelta(minutes=2))
    status = {row["source"]: row for row in
              source_statuses(settings, path=path,
                              now=now + timedelta(minutes=2))}
    assert status["broken"]["status"] == "healthy"
    assert status["broken"]["error"] is None


def test_initial_interval_skip_seeds_from_existing_evidence(conn, tmp_path):
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO observations (observation_id, source, feed_url, retrieved_at,"
        " payload_hash, raw_payload) VALUES (?,?,?,?,?,?)",
        ("obs_health", "no_storms", "fixture://source", now.isoformat(),
         "hash_health", "{}"))
    path = tmp_path / "health.json"
    settings = _settings(tmp_path)
    record_poll(settings, {"sources": {
        "no_storms": {"ok": True, "skipped": "interval"},
    }}, path=path, now=now + timedelta(minutes=1), conn=conn)
    statuses = {row["source"]: row for row in
                source_statuses(settings, path=path,
                                now=now + timedelta(minutes=1))}
    assert statuses["no_storms"]["status"] == "healthy"
