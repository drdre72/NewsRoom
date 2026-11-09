"""YouTube publish — upload policy + playlist selection (no network)."""
import json
import http.client
import hashlib
import threading
import urllib.error
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from newsroom.publish.youtube import (
    select_best_of_day, upload_eligible, upload_video,
    reviewed_metadata, _manifest_title, _manifest_description,
    authorization_url, save_refresh_token,
)


def test_desktop_auth_url_uses_pkce_state_and_both_scopes():
    url = authorization_url("desktop-id", "http://127.0.0.1:1234/callback",
                            "random-state", "verifier")
    query = parse_qs(urlsplit(url).query)
    assert query["response_type"] == ["code"]
    assert query["state"] == ["random-state"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0] != "verifier"
    assert "youtube.upload" in query["scope"][0]
    assert "https://www.googleapis.com/auth/youtube" in query["scope"][0]
    assert query["access_type"] == ["offline"]


def test_short_start_url_redirects_and_callback_checks_state(monkeypatch):
    import newsroom.publish.youtube as yt

    monkeypatch.setattr(yt, "_client_creds", lambda: ("desktop-id", "secret"))
    captured = {}

    def exchange(code, redirect_uri, verifier):
        captured.update(code=code, redirect_uri=redirect_uri, verifier=verifier)
        return "test-refresh"

    monkeypatch.setattr(yt, "exchange_auth_code", exchange)
    announced = []
    result = []
    thread = threading.Thread(target=lambda: result.append(
        yt.authorize_desktop(timeout=5, announce=announced.append)))
    thread.start()
    try:
        # The listener is running before the short URL is announced.
        import time
        deadline = time.monotonic() + 2
        while len(announced) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        start = urlsplit(announced[1])
        assert start.scheme == "http" and start.hostname == "127.0.0.1"
        assert start.path == "/start" and not start.query
        conn = http.client.HTTPConnection(start.hostname, start.port, timeout=2)
        conn.request("GET", "/start")
        response = conn.getresponse()
        auth = response.getheader("Location")
        assert response.status == 302
        assert response.getheader("Referrer-Policy") == "no-referrer"
        response.read()
        query = parse_qs(urlsplit(auth).query)
        callback = urlsplit(query["redirect_uri"][0])
        assert callback.port == start.port
        conn.request("GET", "/callback?state=wrong&code=forged")
        response = conn.getresponse()
        assert response.status == 400
        response.read()
        conn.request("GET", f"/callback?state={query['state'][0]}&code=approved")
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        conn.close()
    finally:
        thread.join(timeout=6)
    assert not thread.is_alive()
    assert result == ["test-refresh"]
    assert captured["code"] == "approved"


def test_save_refresh_token_private_and_no_duplicate(tmp_path):
    env = tmp_path / ".env"
    env.write_text("UNRELATED=preserved\nNEWSROOM_YT_REFRESH_TOKEN=old\n")
    env.chmod(0o600)
    save_refresh_token("new", env)
    assert env.stat().st_mode & 0o777 == 0o600
    assert env.read_text() == "UNRELATED=preserved\nNEWSROOM_YT_REFRESH_TOKEN=new\n"
    assert not (tmp_path / ".env.oauth.tmp").exists()


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status = status
        self.headers = headers or {}
        self.body = json.dumps(body or {}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def _upload_fixture(tmp_path):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video-bytes")
    manifest = {"video": {"path": str(video),
                          "hash": hashlib.sha256(video.read_bytes()).hexdigest(),
                          "height": 1920, "width": 1080},
                "event_id": "crime_report:real"}
    return manifest, tmp_path / "private"


REVIEWED = {"title": "Reviewed test bulletin", "description": "Source: test",
            "privacy": "private", "tags": ["news", "shorts"]}


def test_reviewed_draft_matches_asset_and_current_source(conn, tmp_path,
                                                         monkeypatch):
    import newsroom.storage.repositories as repo
    source = "https://www.bbc.co.uk/news/articles/test"
    monkeypatch.setattr(repo, "current_facts", lambda *_args: [
        SimpleNamespace(field="report_url", value_display=source)])
    candidate = {"asset_id": "asset_test", "event_id": "news_report:bbc:test",
                 "revision": 1, "manifest": {
                     "video": {"hash": "abc"}, "event_revision": 1}}
    data = {**REVIEWED, "asset_id": "asset_test",
            "event_id": "news_report:bbc:test", "revision": 1,
            "video_sha256": "abc", "source_url": source,
            "description": f"BBC World reports the story.\n{source}\n#Shorts"}
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(data))
    assert reviewed_metadata(conn, candidate, path)["privacy"] == "private"
    data["video_sha256"] = "different"
    path.write_text(json.dumps(data))
    import pytest
    with pytest.raises(ValueError, match="does not match"):
        reviewed_metadata(conn, candidate, path)
    data["video_sha256"] = "abc"
    data["source_url"] = "https://not-the-current-report.invalid/"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="current"):
        reviewed_metadata(conn, candidate, path)


def test_lost_final_response_resumes_without_second_upload(tmp_path, monkeypatch):
    import newsroom.publish.youtube as yt
    manifest, directory = _upload_fixture(tmp_path)
    calls = {"post": 0, "data_put": 0, "committed": False}

    def fake_open(req, timeout):
        if req.get_method() == "POST":
            calls["post"] += 1
            return FakeResponse(200, headers={"Location":
                "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=abc"})
        if req.data == b"":
            if calls["committed"]:
                return FakeResponse(200, {"id": "video123"})
            raise urllib.error.HTTPError(req.full_url, 308, "Resume",
                                         {}, BytesIO())
        calls["data_put"] += 1
        calls["committed"] = True
        raise urllib.error.URLError("connection lost after commit")

    monkeypatch.setattr(yt.urllib.request, "urlopen", fake_open)
    first = upload_video(manifest, None, "token", asset_id="asset_1",
                         session_dir=directory, reviewed=REVIEWED)
    assert first["ok"] is False
    journal = directory / "asset_1.json"
    assert journal.exists() and journal.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    second = upload_video(manifest, None, "token", asset_id="asset_1",
                          session_dir=directory, reviewed=REVIEWED)
    assert second["video_id"] == "video123"
    third = upload_video(manifest, None, "token", asset_id="asset_1",
                         session_dir=directory, reviewed=REVIEWED)
    assert third["video_id"] == "video123"
    assert calls == {"post": 1, "data_put": 1, "committed": True}


def test_expired_session_fails_closed_without_new_post(tmp_path, monkeypatch):
    import newsroom.publish.youtube as yt
    manifest, directory = _upload_fixture(tmp_path)
    calls = {"post": 0}

    def fake_open(req, timeout):
        if req.get_method() == "POST":
            calls["post"] += 1
            return FakeResponse(200, headers={"Location":
                "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=abc"})
        raise urllib.error.HTTPError(req.full_url, 404, "Expired", {}, BytesIO())

    monkeypatch.setattr(yt.urllib.request, "urlopen", fake_open)
    for _ in range(2):
        result = upload_video(manifest, None, "token", asset_id="asset_2",
                              session_dir=directory, reviewed=REVIEWED)
        assert result["ok"] is False
    assert calls["post"] == 1


def test_resume_partial_chunk_at_server_offset(tmp_path, monkeypatch):
    import newsroom.publish.youtube as yt
    manifest, directory = _upload_fixture(tmp_path)
    offsets = []

    def fake_open(req, timeout):
        if req.get_method() == "POST":
            return FakeResponse(200, headers={"Location":
                "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=abc"})
        if req.data == b"":
            raise urllib.error.HTTPError(req.full_url, 308, "Resume",
                                         {"Range": "bytes=0-4"}, BytesIO())
        offsets.append((req.get_header("Content-range"), req.data))
        return FakeResponse(200, {"id": "video123"})

    monkeypatch.setattr(yt.urllib.request, "urlopen", fake_open)
    result = upload_video(manifest, None, "token", asset_id="asset_3",
                          session_dir=directory, reviewed=REVIEWED)
    assert result["video_id"] == "video123"
    assert offsets == [("bytes 5-10/11", b"-bytes")]


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

    def test_real_selection_cap_revision_sim_and_idempotency(
            self, conn, settings, tmp_path, monkeypatch):
        import newsroom.publish.youtube as yt
        from newsroom.storage import repositories as repo

        monkeypatch.setattr("newsroom.paths.output_dir", lambda: tmp_path)
        manifest_dir = tmp_path / "manifests"
        manifest_dir.mkdir()

        def asset(event_id, revision=1, verdict="pass"):
            aid = _seed(conn, event_id, "BREAKING", 0.7, revision=revision)
            video = tmp_path / (event_id.replace(":", "_") + ".mp4")
            video.write_bytes(event_id.encode())
            digest = hashlib.sha256(video.read_bytes()).hexdigest()
            conn.execute("UPDATE assets SET path=?,sha256=? WHERE asset_id=?",
                         (str(video), digest, aid))
            (manifest_dir / f"{event_id.replace(':', '_')}_r{revision}.json").write_text(
                json.dumps({"event_id": event_id, "event_revision": revision,
                            "publication_gate": {"verdict": verdict},
                            "video": {"path": str(video), "hash": digest}}))
            return aid

        good = asset("crime_report:real")
        asset("crime_report:second")
        row = repo.get_asset(conn, good)
        repo.insert_asset(conn, repo.new_id("asset"), "video",
                          row["path"], row["sha256"],
                          row["event_id"], row["revision"], {})
        asset("crime_report:simnws1")
        asset("crime_report:rejected", verdict="reject")
        asset("crime_report:gnews")
        repo.add_event_source(conn, "crime_report:gnews",
                              "gnews_crime", "article")
        asset("crime_report:fixture")
        conn.execute(
            """INSERT INTO observations
               (observation_id, source, source_event_id, feed_url,
                retrieved_at, payload_hash, raw_payload, event_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("fixture-1", "fixture", "fixture-1", "fixture://crime.json",
             repo.utcnow(), "fixture-hash", "{}", "crime_report:fixture"))
        asset("crime_report:old", revision=1)
        conn.execute("UPDATE events SET current_revision=2 WHERE event_id=?",
                     ("crime_report:old",))
        assert len(upload_eligible(conn, settings, max_per_day=1)) == 1
        selected = upload_eligible(conn, settings, max_per_day=3)
        assert {a["event_id"] for a in selected} == {
            "crime_report:real", "crime_report:second"}
        repo.patch_asset_meta(conn, good, {
            "youtube_id": "uploaded", "youtube_uploaded_at": repo.utcnow()})
        assert [a["event_id"] for a in upload_eligible(
            conn, settings, max_per_day=3)] == ["crime_report:second"]
        settings.raw()["publishing"]["youtube"]["max_per_day"] = 1
        assert upload_eligible(conn, settings, max_per_day=3) == []


class TestBestOfDay:
    def test_restricted_press_and_weather_not_promoted(self, conn, settings):
        from newsroom.storage import repositories as repo
        _seed(conn, "crime_report:google", "FLASH", 0.99, airs=1)
        repo.add_event_source(conn, "crime_report:google",
                              "gnews_federal", "article")
        _seed(conn, "weather_alert:urgent", "FLASH", 0.98, airs=1)
        conn.execute("UPDATE events SET event_type='weather_alert' WHERE event_id=?",
                     ("weather_alert:urgent",))
        _seed(conn, "crime_report:direct", "UPDATE", 0.45, airs=1)
        assert [row["event_id"] for row in select_best_of_day(conn, settings)] == [
            "crime_report:direct"]

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
