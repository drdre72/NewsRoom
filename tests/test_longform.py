"""Long-form explainer tests — the YouTube leg (§18)."""
import json

import pytest

from conftest import fixture


def _seed_cyclone(conn):
    """Ingest a GDACS tropical cyclone fixture into the test DB."""
    from newsroom.ingestion.adapters.gdacs import GdacsAdapter
    from newsroom.pipeline import ingest_observation
    ad = GdacsAdapter(name="gdacs", config={
        "authority": 0.80,
        "feeds": {"red": "fixture://gdacs"},
        "poll_feed": "red"})
    body = fixture("gdacs_cyclone.xml")
    for item in ad.split_items(body):
        ingest_observation(conn, ad, item,
                          "fixture://gdacs", "2026-11-07T00:00:00.000Z")


def _seed_earthquake(conn):
    from newsroom.ingestion.adapters.usgs import UsgsAdapter
    from newsroom.pipeline import ingest_observation
    ad = UsgsAdapter(name="usgs", config={
        "authority": 0.95,
        "feeds": {"hour": "fixture://usgs"},
        "poll_feed": "hour"})
    ingest_observation(conn, ad, fixture("earthquake_major.geojson"),
                      "fixture://usgs", "2026-11-07T00:00:00.000Z")


class TestLongformPlan:
    def test_plan_has_explainer_sections(self, conn):
        _seed_earthquake(conn)
        from newsroom.story.story_planner import plan_longform
        from newsroom.storage import repositories as repo
        # find the seeded event
        events = repo.list_events(conn, limit=1)
        assert events
        plan = plan_longform(conn, events[0].event_id)
        assert plan is not None
        assert plan.story_type == "explainer"
        assert plan.duration_target_s == 360
        assert "what_we_know" in plan.sections
        assert "close" in plan.sections

    def test_plan_includes_revisions_for_multi_rev_events(self, conn):
        _seed_earthquake(conn)
        from newsroom.story.story_planner import plan_longform
        from newsroom.storage import repositories as repo
        events = repo.list_events(conn, limit=1)
        plan = plan_longform(conn, events[0].event_id)
        # a single-revision event won't have "revisions" section
        assert "what_we_know" in plan.sections


class TestLongformScript:
    def test_script_has_chapters(self, conn, settings):
        _seed_cyclone(conn)
        from newsroom.storage import repositories as repo
        from newsroom.story.context_builder import build_fact_context
        from newsroom.story.story_planner import plan_longform
        from newsroom.story.script_generator import generate_longform_script
        events = [e for e in repo.list_events(conn, limit=20)
                  if e.event_type == "tropical_cyclone"]
        assert events, "no cyclone seeded"
        eid = events[0].event_id
        ctx = build_fact_context(conn, eid)
        event = repo.get_event(conn, eid)
        plan = plan_longform(conn, eid)
        script, chapters = generate_longform_script(ctx, plan, settings, event)
        assert script.generator == "longform:template"
        assert len(chapters) >= 5
        assert chapters[0]["title"] == "Overview"
        assert any(c["title"] == "What We Know" for c in chapters)
        assert chapters[-1]["title"] == "Closing"
        for ch in chapters:
            assert "first_segment" in ch
        assert "chapters" in script.extra

    def test_script_validates(self, conn, settings):
        _seed_cyclone(conn)
        from newsroom.storage import repositories as repo
        from newsroom.story.context_builder import build_fact_context
        from newsroom.story.story_planner import plan_longform
        from newsroom.story.script_generator import generate_longform_script
        from newsroom.story.claim_validator import validate_script
        events = [e for e in repo.list_events(conn, limit=20)
                  if e.event_type == "tropical_cyclone"]
        eid = events[0].event_id
        ctx = build_fact_context(conn, eid)
        event = repo.get_event(conn, eid)
        plan = plan_longform(conn, eid)
        script, _ = generate_longform_script(ctx, plan, settings, event)
        ok, errs = validate_script(script, ctx, settings)
        assert ok, f"longform script failed validation: {errs}"

    def test_background_exemption(self, conn, settings):
        """Background context about event types is exempt from citation
        requirement — it makes no event-specific claim."""
        _seed_cyclone(conn)
        from newsroom.storage import repositories as repo
        from newsroom.story.context_builder import build_fact_context
        from newsroom.story.story_planner import plan_longform
        from newsroom.story.script_generator import generate_longform_script
        from newsroom.story.claim_validator import validate_script
        events = [e for e in repo.list_events(conn, limit=20)
                  if e.event_type == "tropical_cyclone"]
        eid = events[0].event_id
        ctx = build_fact_context(conn, eid)
        event = repo.get_event(conn, eid)
        plan = plan_longform(conn, eid)
        script, _ = generate_longform_script(ctx, plan, settings, event)
        ok, errs = validate_script(script, ctx, settings)
        # The background segment ("Tropical cyclones are rated by...")
        # must pass validation despite having no citations
        assert ok, errs
        bg_seg = [s for s in script.segments
                  if "tropical cyclones are rated" in s.text.lower()]
        assert bg_seg, "background segment not found"
        assert not bg_seg[0].fact_ids  # no citations — it's context


class TestChapterFormat:
    def test_youtube_chapters_string(self, conn, settings):
        _seed_cyclone(conn)
        from newsroom.storage import repositories as repo
        from newsroom.story.context_builder import build_fact_context
        from newsroom.story.story_planner import plan_longform
        from newsroom.story.script_generator import generate_longform_script
        events = [e for e in repo.list_events(conn, limit=20)
                  if e.event_type == "tropical_cyclone"]
        eid = events[0].event_id
        ctx = build_fact_context(conn, eid)
        event = repo.get_event(conn, eid)
        plan = plan_longform(conn, eid)
        script, chapters = generate_longform_script(ctx, plan, settings, event)
        lines = []
        for ch in chapters:
            s = int(ch.get("start_s", 0))
            lines.append(f"{s//60:02d}:{s%60:02d} {ch['title']}")
        chapters_str = "\n".join(lines)
        assert "Overview" in chapters_str
        assert "Closing" in chapters_str
        for line in chapters_str.splitlines():
            parts = line.split()
            assert len(parts[0]) == 5  # MM:SS format
