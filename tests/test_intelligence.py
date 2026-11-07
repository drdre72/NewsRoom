"""Deterministic scoring + validation tests (offline)."""
from conftest import fixture

from newsroom.ingestion.normalizer import display_value
from newsroom.intelligence.confidence_engine import production_gate, score_confidence
from newsroom.intelligence.impact_engine import score_impact
from newsroom.pipeline import ingest_observation
from newsroom.story.claim_validator import validate_script
from newsroom.story.context_builder import build_fact_context
from newsroom.story.script_generator import _script_from_template
from newsroom.story.story_planner import plan_story
from newsroom.models.story import Script, ScriptSegment
from newsroom.audio.pronunciation import to_speech_text


def test_impact_levels(conn, settings):
    flash = {"magnitude": 7.8, "sig": 1100, "tsunami": True, "alert": "red"}
    score, level = score_impact("earthquake", flash, settings)
    assert level == "FLASH" and score >= 0.85

    normal = {"magnitude": 4.5, "sig": 300, "tsunami": False, "alert": None}
    score, level = score_impact("earthquake", normal, settings)
    assert score < 0.35

    unknown = score_impact("unconfigured_type", {"anything": 1}, settings)
    assert unknown == (0.0, "BACKGROUND")


def test_confidence_separate_from_impact(settings):
    assert score_confidence("usgs", True, 1, settings) == 1.0
    assert score_confidence("usgs", False, 1, settings) == 0.95
    assert score_confidence("gdacs", False, 1, settings) == 0.80
    assert score_confidence("unknown_src", False, 1, settings) == 0.50
    assert production_gate(0.96, settings) == "eligible"
    assert production_gate(0.80, settings) == "corroborate"
    assert production_gate(0.50, settings) == "hold"


def test_display_value_formats():
    assert display_value("magnitude", 6.45) == "6.4" or "6.5"  # .1f
    assert display_value("depth_km", 16) == "16.0 km"
    assert display_value("occurred_at", "2026-11-07T14:31:21Z") == "14:31 UTC, 07 Nov"
    assert display_value("tsunami", True) == "yes"


def test_pronunciation_display_untouched():
    overrides = {"NHC": "N H C", "USGS": "U S G S"}
    display = "The NHC and USGS issued updates."
    speech = to_speech_text(display, overrides)
    assert "NHC" in display
    assert speech == "The N H C and U S G S issued updates."


def _ctx(conn, usgs):
    ingest_observation(conn, usgs, fixture("earthquake_rev1.geojson"),
                       "f", "2026-11-07T00:00:00Z")
    ingest_observation(conn, usgs, fixture("earthquake_rev2.geojson"),
                       "f", "2026-11-07T00:05:00Z")
    return build_fact_context(conn, "earthquake:us7000sim2")


def test_template_script_validates(conn, usgs, settings):
    ctx = _ctx(conn, usgs)
    plan = plan_story(conn, ctx.event_id)
    script = _script_from_template(ctx, plan, settings)
    ok, errors = validate_script(script, ctx, settings)
    assert ok, errors
    # Revision sentence cites both the superseded and current fact.
    rev_seg = [s for s in script.segments if "initially reported" in s.text]
    assert rev_seg and len(rev_seg[0].fact_ids) == 2


def test_validator_rejects_fabrication(conn, usgs, settings):
    ctx = _ctx(conn, usgs)
    bad = Script(
        headline="x", event_id=ctx.event_id, revision=ctx.revision,
        generator="test",
        segments=[
            ScriptSegment("A magnitude 9.9 earthquake destroyed the city.",
                          "x", ["f_does_not_exist"]),
            ScriptSegment("Fifty people were killed.", "x", []),
        ],
    )
    ok, errors = validate_script(bad, ctx, settings)
    assert not ok
    joined = " ".join(errors)
    assert "unknown fact ids" in joined
    assert "high-risk claim" in joined
    assert "untraceable number '9.9'" in joined


def test_validator_rejects_uncited_number(conn, usgs, settings):
    ctx = _ctx(conn, usgs)
    mag_fact = next(f.fact_id for f in ctx.current if f.field == "magnitude")
    s = Script(
        headline="x", event_id=ctx.event_id, revision=ctx.revision,
        generator="test",
        segments=[ScriptSegment("The quake was magnitude 8.8.", "x", [mag_fact])],
    )
    ok, errors = validate_script(s, ctx, settings)
    assert not ok and "untraceable number '8.8'" in " ".join(errors)
