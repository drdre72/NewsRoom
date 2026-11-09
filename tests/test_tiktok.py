"""TikTok post desk — manual draft packages, captions, eligibility."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from newsroom.publish.tiktok import (
    build_caption, build_checklist, build_draft_package, draft_candidates,
    list_candidates, mark_posted, rewards_eligible,
)


def _seed_event(conn, event_id, *, event_type="news_report",
                impact="BREAKING", score=0.7, conf=0.8, revision=1,
                title=None):
    from newsroom.models.event import CanonicalEvent
    from newsroom.storage import repositories as repo
    ev = CanonicalEvent(
        event_id=event_id, event_type=event_type, status="active",
        severity=5.0, confidence=conf, first_seen=repo.utcnow(),
        last_updated=repo.utcnow(), title=title or f"Test {event_id}",
        impact_score=score, impact_level=impact,
        confidence_score=conf, current_revision=revision)
    repo.upsert_event(conn, ev)
    return ev


def _seed_tiktok_asset(conn, tmp_path, event_id, revision=1, meta=None,
                       verdict="pass", duration_s=75.0):
    """Insert a tiktok-format video asset + matching manifest."""
    from newsroom.storage import repositories as repo
    video = tmp_path / f"{event_id.replace(':', '_')}_tiktok.mp4"
    video.write_bytes(event_id.encode())
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    aid = repo.new_id("asset")
    repo.insert_asset(conn, aid, "video", str(video), digest, event_id,
                      revision, {"format": "tiktok", **(meta or {})})
    mdir = tmp_path / "manifests"
    mdir.mkdir(exist_ok=True)
    safe = event_id.replace(":", "_")
    (mdir / f"{safe}_r{revision}_tiktok.json").write_text(json.dumps({
        "event_id": event_id, "event_revision": revision,
        "format": "tiktok",
        "publication_gate": {"verdict": verdict},
        "captions": [[3.5, 9.0, "BBC World reports: the headline detail"]],
        "sources": ["bbc_world:abc"],
        "video": {"path": str(video), "hash": digest, "width": 1080,
                  "height": 1920, "fps": 30, "duration_s": duration_s}}))
    return aid


def _event(**kw):
    base = {"title": "BBC World: Houthis say 25 killed",
            "event_type": "news_report", "location_label": None,
            "impact_level": "BREAKING"}
    return SimpleNamespace(**{**base, **kw})


class TestCaption:
    def test_shape_attribution_disclosure_no_urls(self, settings):
        manifest = {"captions": [[3.5, 9.0,
                                  "BBC World reports: the headline detail"]],
                    "sources": ["bbc_world:x"], "event_id": "news_report:x"}
        facts = [SimpleNamespace(field="outlet", value_display="BBC World"),
                 SimpleNamespace(
                     field="report_url",
                     value_display="https://www.bbc.co.uk/news/articles/abc")]
        caption = build_caption(manifest, _event(), facts, settings)
        assert len(caption) <= 2200
        assert caption.startswith("BBC World: Houthis say")
        assert "BBC World reports" in caption
        assert "Source: BBC World" in caption
        assert "bbc.co.uk" in caption
        assert "http" not in caption      # links are dead text on TikTok
        assert "AI-narrated autonomous newsroom" in caption
        assert "@justsystemnews" in caption
        assert 1 <= caption.count("#") <= 5

    def test_drops_lede_before_truncating(self, settings):
        manifest = {"captions": [[0, 1, "lede " * 100]],
                    "sources": ["s:1"], "event_id": "e"}
        cap = build_caption(manifest, _event(title="X" * 1900),
                            [], settings)
        assert len(cap) <= 2200
        assert "lede" not in cap
        assert "#news" in cap

    def test_hashtag_bounds_and_types(self, settings):
        manifest = {"captions": [], "sources": [], "event_id": "e"}
        cap = build_caption(manifest, _event(
            event_type="earthquake", location_label="San Francisco"), [],
            settings)
        assert "#earthquake" in cap and "#SanFrancisco" in cap
        assert "#breaking" in cap            # BREAKING impact
        assert cap.count("#") <= 5

    def test_identical_lede_not_duplicated(self, settings):
        title = "FEMA Major Disaster Declaration: Hurricane Isaias — FL"
        manifest = {"captions": [[3.5, 8.0, title + "."]],
                    "sources": ["fema:x"], "event_id": "e"}
        cap = build_caption(manifest, _event(title=title), [], settings)
        assert cap.count("Hurricane Isaias") == 1

    def test_agency_source_name_pretty(self, settings):
        manifest = {"captions": [], "sources": ["fema:DR-1"],
                    "event_id": "e"}
        cap = build_caption(manifest, _event(), [], settings)
        assert "Source: FEMA" in cap


class TestChecklist:
    def test_requires_aigc_and_upload_flow(self, settings, tmp_path):
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        cand = {"event_id": "news_report:a", "revision": 1,
                "path": str(video),
                "manifest": {"video": {"duration_s": 75, "width": 1080,
                                       "height": 1920},
                             "publication_gate": {"verdict": "pass"}}}
        text = build_checklist(cand, _event(), "cap", True, settings)
        assert "Disclose AI-generated content" in text
        assert "tiktok.com/upload" in text
        assert "tiktok posted news_report:a" in text
        assert "qualifies" in text


class TestDraftCandidates:
    def test_gate_pass_selects_and_rest_skip(self, conn, settings, tmp_path,
                                             monkeypatch):
        monkeypatch.setattr("newsroom.paths.output_dir", lambda: tmp_path)
        from newsroom.storage import repositories as repo

        _seed_event(conn, "news_report:good")
        _seed_tiktok_asset(conn, tmp_path, "news_report:good")

        _seed_event(conn, "news_report:restricted")
        _seed_tiktok_asset(conn, tmp_path, "news_report:restricted")
        repo.add_event_source(conn, "news_report:restricted",
                              "gnews_crime", "article")

        _seed_event(conn, "weather_alert:w", event_type="weather_alert")
        _seed_tiktok_asset(conn, tmp_path, "weather_alert:w")

        _seed_event(conn, "news_report:stale", revision=2)
        _seed_tiktok_asset(conn, tmp_path, "news_report:stale", revision=1)

        _seed_event(conn, "news_report:fix")
        _seed_tiktok_asset(conn, tmp_path, "news_report:fix")
        conn.execute(
            """INSERT INTO observations
               (observation_id, source, source_event_id, feed_url,
                retrieved_at, payload_hash, raw_payload, event_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("fixture-1", "fixture", "fixture-1", "fixture://story.json",
             repo.utcnow(), "fixture-hash", "{}", "news_report:fix"))

        _seed_event(conn, "news_report:fail")
        _seed_tiktok_asset(conn, tmp_path, "news_report:fail",
                           verdict="reject")

        _seed_event(conn, "news_report:sim1")
        _seed_tiktok_asset(conn, tmp_path, "news_report:sim1")

        cands = draft_candidates(conn, settings)
        assert [c["event_id"] for c in cands] == ["news_report:good"]

    def test_drafted_and_posted_excluded(self, conn, settings, tmp_path,
                                         monkeypatch):
        monkeypatch.setattr("newsroom.paths.output_dir", lambda: tmp_path)
        _seed_event(conn, "news_report:d1")
        _seed_tiktok_asset(conn, tmp_path, "news_report:d1",
                           meta={"tiktok_draft_at": "2026-11-09T00:00:00Z"})
        _seed_event(conn, "news_report:p1")
        _seed_tiktok_asset(
            conn, tmp_path, "news_report:p1",
            meta={"tiktok_url": "https://www.tiktok.com/@x/video/1"})
        assert draft_candidates(conn, settings) == []

    def test_daily_cap_blocks_bulk_but_explicit_draft_bypasses(
            self, conn, settings, tmp_path, monkeypatch):
        monkeypatch.setattr("newsroom.paths.output_dir", lambda: tmp_path)
        from newsroom.storage import repositories as repo
        _seed_event(conn, "news_report:cap1")
        aid = _seed_tiktok_asset(conn, tmp_path, "news_report:cap1")
        repo.patch_asset_meta(conn, aid, {"tiktok_draft_at": repo.utcnow()})
        _seed_event(conn, "news_report:cap2")
        _seed_tiktok_asset(conn, tmp_path, "news_report:cap2")
        settings.raw()["publishing"]["tiktok"]["max_drafts_per_day"] = 1
        assert draft_candidates(conn, settings) == []
        assert [c["event_id"] for c in draft_candidates(
            conn, settings, enforce_daily_cap=False)] == [
                "news_report:cap2"]


class TestPackageAndPosting:
    def test_package_layout_and_ledger_stamp(self, conn, settings, tmp_path,
                                             monkeypatch):
        monkeypatch.setattr("newsroom.paths.output_dir", lambda: tmp_path)
        _seed_event(conn, "news_report:pkg", title="BBC World: Big story")
        aid = _seed_tiktok_asset(conn, tmp_path, "news_report:pkg")
        cand = draft_candidates(conn, settings, event_id="news_report:pkg",
                                enforce_daily_cap=False)[0]
        result = build_draft_package(conn, settings, cand,
                                     dest_root=tmp_path / "pkgs")
        d = Path(result["dir"])
        assert (d / "video.mp4").read_bytes() == b"news_report:pkg"
        assert (d / "caption.txt").read_text().startswith("BBC World")
        assert "Disclose AI-generated" in (d / "checklist.md").read_text()
        draft = json.loads((d / "draft.json").read_text())
        assert draft["aigc_disclosure_required"] is True
        assert draft["rewards_eligible"] is True     # duration_s=75
        assert draft["status"] == "awaiting_manual_post"
        assert draft["handle"] == "@justsystemnews"
        meta = json.loads(conn.execute(
            "SELECT meta_json FROM assets WHERE asset_id=?",
            (aid,)).fetchone()[0])
        assert meta["tiktok_draft_at"] and meta["tiktok_draft_dir"] == str(d)
        assert draft_candidates(conn, settings) == []   # stamped -> done

    def test_mark_posted_validates_and_records(self, conn, settings,
                                               tmp_path, monkeypatch):
        monkeypatch.setattr("newsroom.paths.output_dir", lambda: tmp_path)
        _seed_event(conn, "news_report:post")
        aid = _seed_tiktok_asset(conn, tmp_path, "news_report:post")
        with pytest.raises(ValueError, match="tiktok.com"):
            mark_posted(conn, "news_report:post", "https://evil.example/x")
        out = mark_posted(conn, "news_report:post",
                          "https://www.tiktok.com/@justsystemnews/video/7")
        assert out["assets"] == 1
        meta = json.loads(conn.execute(
            "SELECT meta_json FROM assets WHERE asset_id=?",
            (aid,)).fetchone()[0])
        assert meta["tiktok_url"].endswith("/video/7")
        assert meta["tiktok_posted_at"]
        with pytest.raises(ValueError, match="no tiktok render"):
            mark_posted(conn, "news_report:absent",
                        "https://www.tiktok.com/x")


class TestListCandidates:
    def test_statuses_and_filters(self, conn, settings, tmp_path,
                                  monkeypatch):
        monkeypatch.setattr("newsroom.paths.output_dir", lambda: tmp_path)
        from newsroom.storage import repositories as repo
        _seed_event(conn, "news_report:new")
        _seed_event(conn, "news_report:ren")
        _seed_tiktok_asset(conn, tmp_path, "news_report:ren")
        _seed_event(conn, "news_report:dr")
        aid = _seed_tiktok_asset(conn, tmp_path, "news_report:dr")
        repo.patch_asset_meta(conn, aid, {"tiktok_draft_at": repo.utcnow()})
        _seed_event(conn, "news_report:po")
        _seed_tiktok_asset(conn, tmp_path, "news_report:po",
                           meta={"tiktok_url": "https://www.tiktok.com/x"})
        _seed_event(conn, "news_report:hold", conf=0.2)
        _seed_event(conn, "weather_alert:w", event_type="weather_alert")
        _seed_event(conn, "news_report:sim_evt")
        rows = {r["event_id"]: r["tiktok"]
                for r in list_candidates(conn, settings)}
        assert rows["news_report:new"] == "new"
        assert rows["news_report:ren"] == "rendered"
        assert rows["news_report:dr"] == "drafted"
        assert rows["news_report:po"] == "posted"
        assert "news_report:hold" not in rows
        assert "weather_alert:w" not in rows
        assert "news_report:sim_evt" not in rows


class TestRewards:
    def test_minute_threshold(self, settings):
        assert rewards_eligible({"video": {"duration_s": 75}}, settings)
        assert rewards_eligible({"video": {"duration_s": 61}}, settings)
        assert not rewards_eligible({"video": {"duration_s": 60}}, settings)
        assert not rewards_eligible({"video": {}}, settings)


def test_render_tiktok_delegates_vertical(monkeypatch):
    import newsroom.render as r
    seen = {}
    monkeypatch.setattr(
        r, "render_longform",
        lambda *a, **kw: seen.update(kw) or {"ok": True})
    assert r.render_tiktok("conn", "settings", "evt") == {"ok": True}
    assert seen["fmt"] == "tiktok"
    assert seen["dry_run"] is False
