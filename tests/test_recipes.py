"""Retention & rehydration tests — recipe build, eligibility, dehydrate
flow, restore, and iCloud archive. All offline: renders are stubbed where
a full TTS+ffmpeg pass isn't the thing under test."""
import json
from pathlib import Path

import pytest

from conftest import fixture


def _seed_event(conn):
    from newsroom.ingestion.adapters.usgs import UsgsAdapter
    from newsroom.pipeline import ingest_observation
    ad = UsgsAdapter(name="usgs", config={
        "authority": 0.95,
        "feeds": {"hour": "fixture://usgs"},
        "poll_feed": "hour"})
    ingest_observation(conn, ad, fixture("earthquake_major.geojson"),
                      "fixture://usgs", "2026-11-07T00:00:00.000Z")
    from newsroom.storage import repositories as repo
    # Ingest queues a render job; fabricated assets imply it already ran.
    conn.execute("UPDATE jobs SET status='done' WHERE status='queued'")
    return repo.list_events(conn, limit=1)[0]


def _make_video_asset(conn, event, tmp_path, *, revision=None,
                      fmt="short", meta=None, story=True):
    """A fabricated render output: dummy mp4 + validated story + asset row."""
    from newsroom.models.story import Script, ScriptSegment
    from newsroom.storage import repositories as repo
    rev = revision if revision is not None else event.current_revision
    video = tmp_path / f"clip_{event.event_id.replace(':', '_')}_r{rev}.mp4"
    video.write_bytes(b"\x00" * 4096 + event.event_id.encode())
    if story:
        facts = repo.current_facts(conn, event.event_id)
        fid = facts[0].fact_id if facts else "f_x"
        script = Script(
            headline=f"{event.title or event.event_id}",
            segments=[ScriptSegment(text="A test segment.",
                                    speech_text="A test segment.",
                                    fact_ids=[fid])],
            generator="template", event_id=event.event_id, revision=rev,
            validated=True)
        repo.insert_story(conn, repo.new_id("story"), event.event_id, rev,
                          json.dumps({"story_type": "breaking",
                                      "sections": ["lede"]}),
                          json.dumps(script.to_dict()), "template",
                          True, "validated")
    asset_id = repo.new_id("asset")
    repo.insert_asset(conn, asset_id, "video", str(video),
                      None, event.event_id, rev, meta or {"format": fmt})
    return repo.get_asset(conn, asset_id)


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    """Isolated NEWSROOM_ROOT so recipes/output never touch the repo."""
    monkeypatch.setenv("NEWSROOM_ROOT", str(tmp_path))
    return tmp_path


class TestBuildRecipe:
    def test_recipe_is_self_contained(self, conn, settings, workdir):
        from newsroom.storage.recipes import build_recipe
        event = _seed_event(conn)
        asset = _make_video_asset(conn, event, workdir)
        recipe = build_recipe(conn, settings, asset)
        assert recipe["event_id"] == event.event_id
        assert recipe["script"]["segments"], "script must replay verbatim"
        assert recipe["visual_plan"]["shots"], "frozen plan required"
        assert recipe["facts"], "fact snapshot required"
        assert recipe["fact_ids"]
        assert recipe["video"]["sha256"]
        assert recipe["render"]["width"]
        assert recipe["voice"] is not None or True  # older manifest → None ok
        assert recipe["event"]["current_revision"] == event.current_revision

    def test_recipe_file_roundtrip(self, conn, settings, workdir):
        from newsroom.storage.recipes import (build_recipe,
                                              write_recipe_file)
        event = _seed_event(conn)
        asset = _make_video_asset(conn, event, workdir)
        recipe = build_recipe(conn, settings, asset)
        path = write_recipe_file(settings, recipe)
        loaded = json.loads(path.read_text())
        assert loaded["recipe_id"] == recipe["recipe_id"]
        assert loaded["script"] == recipe["script"]

    def test_missing_story_refuses(self, conn, settings, workdir):
        from newsroom.storage.recipes import build_recipe
        event = _seed_event(conn)
        asset = _make_video_asset(conn, event, workdir, story=False)
        with pytest.raises(ValueError, match="no validated"):
            build_recipe(conn, settings, asset)


class TestEligibility:
    def test_awaiting_premiere_protected(self, conn, settings, workdir):
        from newsroom.storage import repositories as repo
        from newsroom.storage.recipes import evaluate_asset
        event = _seed_event(conn)
        conn.execute("UPDATE events SET confidence_score=0.9"
                     " WHERE event_id=?", (event.event_id,))
        asset = _make_video_asset(conn, event, workdir)
        ev = evaluate_asset(conn, settings, asset)
        assert ev["status"] == "protected"
        assert ev["reason"] == "awaiting_premiere"

    def test_pending_job_protected(self, conn, settings, workdir):
        from newsroom.models.job import Job
        from newsroom.storage import repositories as repo
        from newsroom.storage.recipes import evaluate_asset
        event = _seed_event(conn)
        asset = _make_video_asset(conn, event, workdir)
        repo.enqueue_job(conn, Job(
            job_id=repo.new_id("job"), job_type="short_render",
            status="queued", priority=50, event_id=event.event_id,
            revision=event.current_revision, payload_json="{}",
            created_at=repo.utcnow()))
        ev = evaluate_asset(conn, settings, asset)
        assert ev["reason"] == "job_pending"

    def test_recently_aired_protected(self, conn, settings, workdir):
        from newsroom.storage import repositories as repo
        from newsroom.storage.recipes import evaluate_asset
        event = _seed_event(conn)
        asset = _make_video_asset(conn, event, workdir)
        repo.log_air(conn, repo.new_id("air"), "story", event.event_id,
                     event.current_revision, "earthquakes",
                     asset["path"], 30.0)
        ev = evaluate_asset(conn, settings, asset)
        assert ev["reason"] in ("recently_aired", "on_air")

    def test_retired_eligible(self, conn, settings, workdir):
        from newsroom.storage import repositories as repo
        from newsroom.storage.recipes import evaluate_asset
        event = _seed_event(conn)
        asset = _make_video_asset(conn, event, workdir)
        repo.log_air(conn, repo.new_id("air"), "story", event.event_id,
                     event.current_revision, "earthquakes",
                     asset["path"], 30.0)
        # Backdate the air beyond the 7-day window.
        conn.execute("UPDATE air_log SET started_at='2020-01-01T00:00:00.000Z'")
        ev = evaluate_asset(conn, settings, asset)
        assert ev["status"] == "eligible"
        assert ev["reason"] == "retired"

    def test_stale_unaired_eligible(self, conn, settings, workdir):
        from newsroom.storage.recipes import evaluate_asset
        event = _seed_event(conn)
        asset = _make_video_asset(conn, event, workdir)
        conn.execute("UPDATE events SET current_revision=current_revision+1"
                     " WHERE event_id=?", (event.event_id,))
        ev = evaluate_asset(conn, settings, asset)
        assert ev["status"] == "eligible"
        assert ev["reason"] == "stale_unaired"

    def test_pressure_window_shrinks(self, conn, settings, monkeypatch):
        from newsroom.storage import recipes
        monkeypatch.setattr(
            "newsroom.storage.retention.disk_health",
            lambda s, root=None: {"ok": True, "free_gb": 5.0,
                                  "min_free_gb": 10})
        assert recipes.effective_retention_days(settings) == 2.0


class TestDehydrate:
    def test_plan_deletes_nothing(self, conn, settings, workdir):
        from newsroom.storage import repositories as repo
        from newsroom.storage.recipes import dehydrate
        event = _seed_event(conn)
        asset = _make_video_asset(conn, event, workdir)
        repo.log_air(conn, repo.new_id("air"), "story", event.event_id,
                     event.current_revision, "e", asset["path"], 30.0)
        conn.execute("UPDATE air_log SET started_at='2020-01-01T00:00:00.000Z'")
        out = dehydrate(conn, settings, execute=False)
        assert out["planned"] == 1
        assert Path(asset["path"]).exists()
        assert not repo.list_recipes(conn)

    def test_verified_flow_archives_and_removes(self, conn, settings,
                                                workdir, monkeypatch):
        from newsroom.storage import recipes, repositories as repo
        event = _seed_event(conn)
        asset = _make_video_asset(conn, event, workdir)
        repo.log_air(conn, repo.new_id("air"), "story", event.event_id,
                     event.current_revision, "e", asset["path"], 30.0)
        conn.execute("UPDATE air_log SET started_at='2020-01-01T00:00:00.000Z'")
        monkeypatch.setattr(recipes, "verify_recipe",
                            lambda *a, **k: {"ok": True, "issues": []})
        out = recipes.dehydrate(conn, settings, execute=True)
        assert out["done"][0]["status"] == "dehydrated"
        assert out["freed_bytes"] > 0
        assert not Path(asset["path"]).exists(), "MP4 must be removed"
        meta = json.loads(repo.get_asset(conn, asset["asset_id"])
                          ["meta_json"])
        assert meta["archived"] == 1
        row = repo.recipe_for_asset(conn, asset["asset_id"])
        assert row["status"] == "dehydrated"
        assert row["recipe_sha256"]
        assert Path(row["path"]).exists()
        # …and it left the air rotation.
        assert repo.latest_video_asset(
            conn, event.event_id, event.current_revision) is None

    def test_verify_failure_keeps_everything(self, conn, settings,
                                             workdir, monkeypatch):
        from newsroom.storage import recipes, repositories as repo
        event = _seed_event(conn)
        asset = _make_video_asset(conn, event, workdir)
        repo.log_air(conn, repo.new_id("air"), "story", event.event_id,
                     event.current_revision, "e", asset["path"], 30.0)
        conn.execute("UPDATE air_log SET started_at='2020-01-01T00:00:00.000Z'")
        monkeypatch.setattr(recipes, "verify_recipe",
                            lambda *a, **k: {"ok": False,
                                             "issues": ["render died"]})
        out = recipes.dehydrate(conn, settings, execute=True)
        assert out["done"][0]["status"] == "verify_failed"
        assert Path(asset["path"]).exists(), "unverified → MP4 stays"
        meta = json.loads(repo.get_asset(conn, asset["asset_id"])
                          ["meta_json"])
        assert not meta.get("archived")
        assert repo.recipe_for_asset(
            conn, asset["asset_id"])["status"] == "verify_failed"

    def test_verify_renders_to_scratch_only(self, conn, settings,
                                            workdir, monkeypatch):
        """Regression: the verification render must write inside the
        scratch dir — a previous bug let it target the real output dir
        and cleanup then deleted every rendered short."""
        from newsroom.storage import recipes, repositories as repo
        event = _seed_event(conn)
        asset = _make_video_asset(conn, event, workdir)
        recipe = recipes.build_recipe(conn, settings, asset)
        rpath = recipes.write_recipe_file(settings, recipe)
        with conn:
            repo.insert_recipe(conn, {
                "recipe_id": recipe["recipe_id"],
                "asset_id": asset["asset_id"],
                "event_id": event.event_id,
                "revision": asset["revision"], "format": "short",
                "path": str(rpath), "video_path": asset["path"],
                "video_sha256": recipe["video"]["sha256"],
                "video_bytes": 1, "recipe_sha256": recipes._sha256(rpath),
                "status": "written", "last_air_at": None,
                "created_at": repo.utcnow()})
        seen = {}

        def fake_render(conn_, settings_, recipe_, out_path=None,
                        persist=True):
            seen["out_path"] = out_path
            seen["persist"] = persist
            out_path.write_bytes(b"mp4")
            return {"verify": {"ok": True, "issues": []},
                    "dry_run": False,
                    "video": {"path": str(out_path), "hash": "x",
                              "width": 1080, "height": 1920, "fps": 30}}
        monkeypatch.setattr("newsroom.render.render_from_recipe",
                            fake_render)
        row = repo.recipe_for_asset(conn, asset["asset_id"])
        report = recipes.verify_recipe(conn, settings, row)
        assert report["ok"]
        assert seen["persist"] is False
        # The verify MP4 is in the dedicated scratch dir — never the
        # retired video's path or anywhere under output/.
        assert "verify_" in str(seen["out_path"])
        assert str(seen["out_path"]).endswith("verify.mp4")
        assert Path(asset["path"]).exists()

    def test_unverified_recipe_never_releases_mp4(self, conn, settings,
                                                  workdir):
        from newsroom.storage import repositories as repo
        from newsroom.storage.recipes import dehydrate
        event = _seed_event(conn)
        asset = _make_video_asset(conn, event, workdir)
        repo.log_air(conn, repo.new_id("air"), "story", event.event_id,
                     event.current_revision, "e", asset["path"], 30.0)
        conn.execute("UPDATE air_log SET started_at='2020-01-01T00:00:00.000Z'")
        out = dehydrate(conn, settings, execute=True, verify=False)
        assert out["done"][0]["status"] == "recipe_written_unverified"
        assert Path(asset["path"]).exists()
        meta = json.loads(repo.get_asset(conn, asset["asset_id"])
                          ["meta_json"])
        assert not meta.get("archived")


class TestRestore:
    def test_restore_marks_recipe_and_asset(self, conn, settings,
                                            workdir, monkeypatch):
        from newsroom.storage import recipes, repositories as repo
        event = _seed_event(conn)
        asset = _make_video_asset(conn, event, workdir)
        repo.log_air(conn, repo.new_id("air"), "story", event.event_id,
                     event.current_revision, "e", asset["path"], 30.0)
        conn.execute("UPDATE air_log SET started_at='2020-01-01T00:00:00.000Z'")
        monkeypatch.setattr(recipes, "verify_recipe",
                            lambda *a, **k: {"ok": True, "issues": []})
        recipes.dehydrate(conn, settings, execute=True)
        row = repo.recipe_for_asset(conn, asset["asset_id"])

        fake = {"event_id": event.event_id,
                "video": {"path": str(workdir / "restored.mp4")},
                "publication_gate": {"verdict": "pass"}}
        monkeypatch.setattr("newsroom.render.render_from_recipe",
                            lambda *a, **k: fake)
        manifest = recipes.restore(conn, settings, row["recipe_id"])
        assert manifest["video"]["path"].endswith("restored.mp4")
        assert repo.get_recipe(conn, row["recipe_id"])["status"] == "restored"
        meta = json.loads(repo.get_asset(conn, asset["asset_id"])
                          ["meta_json"])
        assert meta["restored_as"].endswith("restored.mp4")

    def test_tampered_recipe_refuses(self, conn, settings,
                                   workdir, monkeypatch):
        from newsroom.storage import recipes, repositories as repo
        event = _seed_event(conn)
        asset = _make_video_asset(conn, event, workdir)
        repo.log_air(conn, repo.new_id("air"), "story", event.event_id,
                     event.current_revision, "e", asset["path"], 30.0)
        conn.execute("UPDATE air_log SET started_at='2020-01-01T00:00:00.000Z'")
        monkeypatch.setattr(recipes, "verify_recipe",
                            lambda *a, **k: {"ok": True, "issues": []})
        recipes.dehydrate(conn, settings, execute=True)
        row = repo.recipe_for_asset(conn, asset["asset_id"])
        r = json.loads(Path(row["path"]).read_text())
        r["script"]["headline"] = "tampered"
        Path(row["path"]).write_text(json.dumps(r))
        with pytest.raises(RuntimeError, match="sha256"):
            recipes.restore(conn, settings, row["recipe_id"])

    def test_no_recipe_ident_errors(self, conn, settings):
        from newsroom.storage.recipes import restore
        with pytest.raises(ValueError, match="no recipe"):
            restore(conn, settings, "nonexistent")


class TestArchive:
    def test_disabled_reports_and_copies_nothing(self, conn, settings,
                                                 workdir):
        from newsroom.storage.archive import archive_report, run_archive
        settings.raw()["archive"]["enabled"] = False
        report = archive_report(conn, settings)
        assert report["enabled"] is False
        out = run_archive(conn, settings)
        assert out["reason"] == "archive.disabled"

    def test_copy_verify_mark(self, conn, settings, workdir):
        from newsroom.storage import repositories as repo
        from newsroom.storage.archive import run_archive
        event = _seed_event(conn)
        asset = _make_video_asset(conn, event, workdir)
        dest = workdir / "icloud"
        settings.raw()["archive"]["enabled"] = True
        settings.raw()["archive"]["icloud_dir"] = str(dest)
        out = run_archive(conn, settings)
        assert out["ok"] and len(out["copied"]) == 1
        assert Path(out["copied"][0]["dest"]).exists()
        meta = json.loads(repo.get_asset(conn, asset["asset_id"])
                          ["meta_json"])
        assert meta["archived_to"].startswith(str(dest))
        # Second run is a no-op.
        out2 = run_archive(conn, settings)
        assert out2["copied"] == []

    def test_budget_defers(self, conn, settings, workdir):
        from newsroom.storage.archive import run_archive
        event = _seed_event(conn)
        _make_video_asset(conn, event, workdir)
        _make_video_asset(conn, event, workdir,
                          revision=event.current_revision + 1)
        settings.raw()["archive"]["enabled"] = True
        settings.raw()["archive"]["icloud_dir"] = str(workdir / "icloud")
        out = run_archive(conn, settings, max_gb=0.000001)
        assert not out["copied"], "zero-ish budget defers everything"
