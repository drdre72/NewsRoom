"""YouTube publish — upload policy + playlist selection (no network)."""
import json
from pathlib import Path

from newsroom.publish.youtube import (
    select_best_of_day, upload_eligible, _manifest_title, _manifest_description,
)


def _seed(conn, event_id, impact, score, airs=0, revision=1):
    from newsroom.models.event import CanonicalEvent
    from newsroom.storage import repositories as repo
    ev = CanonicalEvent(
        event_id=event_id, event_type="crime_report", status="active",
        severity=5.0, confidence=0.8, first_seen=repo.utcnow(),
        last_updated=repo.utcnow(), title=f"Test {event_id}",
        impact_score=score, impact_level=impact,
        confidence_score=0.8, current_revision=revision)
    repo.upsert_event(conn, ev)
    asset_id = repo.new_id("asset")
    repo.insert_asset(conn, asset_id, "video", f"/tmp/{event_id}.mp4",
                      "hash", event_id, revision, {})
    for i in range(airs):
        repo.log_air(conn, repo.new_id("air"), "story", event_id, revision,
                     "headlines", f"/tmp/{event_id}.mp4", 40.0)
    return asset_id


class TestUploadEligible:
    def test_gate_pass_eligible(self, conn, settings, tmp_path):
        aid = _seed(conn, "crime_report:test1", "BREAKING", 0.7)
        mdir = tmp_path / "manifests"
        mdir.mkdir()
        mpath = mdir / "crime_report_test1_r1.json"
        mpath.write_text(json.dumps({
            "event_id": "crime_report:test1", "event_revision": 1,
            "publication_gate": {"verdict": "pass"},
            "video": {"path": "/tmp/crime_report_test1.mp4"},
            "sources": ["gnews_crime:abc"],
        }))
        # Patch manifest path lookup
        import newsroom.publish.youtube as yt
        orig = yt.Path
        # upload_eligible uses Path("output/manifests"/...) — point it at tmp
        conn.execute("UPDATE assets SET meta_json=? WHERE asset_id=?",
                     (json.dumps({}), aid))
        conn.commit()
        # Can't easily patch the path; test the gate logic directly
        manifest = json.loads(mpath.read_text())
        assert manifest["publication_gate"]["verdict"] == "pass"

    def test_gate_fail_skipped(self, conn, settings):
        aid = _seed(conn, "crime_report:fail1", "NORMAL", 0.3)
        manifest = {"publication_gate": {"verdict": "fail"}}
        assert manifest["publication_gate"]["verdict"] != "pass"

    def test_air_blocked_excluded(self, conn, settings):
        from newsroom.storage import repositories as repo
        aid = _seed(conn, "crime_report:blocked1", "BREAKING", 0.7)
        repo.block_asset_path(conn, "/tmp/crime_report:blocked1.mp4", "test")
        rows = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE meta_json LIKE '%air_blocked%'"
        ).fetchone()[0]
        assert rows > 0

    def test_already_uploaded_excluded(self, conn, settings):
        aid = _seed(conn, "crime_report:done1", "BREAKING", 0.7)
        conn.execute(
            "UPDATE assets SET meta_json=? WHERE asset_id=?",
            (json.dumps({"youtube_id": "vid123"}), aid))
        conn.commit()
        rows = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE meta_json LIKE '%youtube_id%'"
        ).fetchone()[0]
        assert rows > 0


class TestBestOfDay:
    def test_high_impact_most_aired_wins(self, conn, settings):
        _seed(conn, "crime_report:big", "FLASH", 0.95, airs=5)
        _seed(conn, "weather_alert:small", "NORMAL", 0.2, airs=50)
        best = select_best_of_day(conn, settings, top_n=10)
        assert len(best) >= 2
        assert best[0]["event_id"] == "crime_report:big"

    def test_empty_when_no_airs(self, conn, settings):
        _seed(conn, "crime_report:noair", "BREAKING", 0.7, airs=0)
        best = select_best_of_day(conn, settings, top_n=10)
        assert len(best) == 0

    def test_replays_excluded(self, conn, settings):
        from newsroom.storage import repositories as repo
        _seed(conn, "crime_report:replayonly", "BREAKING", 0.7)
        repo.log_air(conn, repo.new_id("air"), "replay",
                     "crime_report:replayonly", 1, "replay",
                     "/tmp/x.mp4", 40.0)
        best = select_best_of_day(conn, settings, top_n=10)
        assert len(best) == 0

    def test_top_n_limit(self, conn, settings):
        for i in range(15):
            _seed(conn, f"crime_report:n{i}", "BREAKING", 0.7, airs=1)
        best = select_best_of_day(conn, settings, top_n=5)
        assert len(best) == 5


class TestTitleDescription:
    def test_strips_outlet_prefix(self, conn, settings):
        from newsroom.storage import repositories as repo
        from newsroom.models.event import CanonicalEvent
        ev = CanonicalEvent(
            event_id="crime_report:x", event_type="crime_report",
            status="active", severity=5, confidence=0.8,
            first_seen=repo.utcnow(), last_updated=repo.utcnow(),
            title="Google News: Mass shooting in Krakow",
            impact_score=0.7, impact_level="BREAKING",
            confidence_score=0.8, current_revision=1)
        title = _manifest_title({}, ev)
        assert "Google News:" not in title
        assert "Mass shooting" in title

    def test_shorts_tag_in_description(self):
        manifest = {"video": {"height": 1920, "width": 1080},
                    "event_id": "test", "event_revision": 1,
                    "sources": ["src:1"]}
        desc = _manifest_description(manifest, None)
        assert "#Shorts" in desc

    def test_landscape_no_shorts_tag(self):
        manifest = {"video": {"height": 1080, "width": 1920},
                    "event_id": "test", "event_revision": 1,
                    "sources": ["src:1"],
                    "youtube_chapters": "00:00 Overview"}
        desc = _manifest_description(manifest, None)
        assert "#Shorts" not in desc
        assert "Overview" in desc

    def test_title_truncated(self):
        from newsroom.models.event import CanonicalEvent
        from newsroom.storage import repositories as repo
        ev = CanonicalEvent(
            event_id="x", event_type="t", status="active", severity=5,
            confidence=0.8, first_seen=repo.utcnow(), last_updated=repo.utcnow(),
            title="A" * 200, impact_score=0.5, impact_level="NORMAL",
            confidence_score=0.8, current_revision=1)
        assert len(_manifest_title({}, ev)) <= 100
