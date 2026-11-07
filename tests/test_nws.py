"""NWS adapter tests — the second event family (§86).

Proves the event abstraction accepts weather alerts without pipeline changes:
same ingest path, same ledger, same validator — only adapter + config differ.
"""
import json

from conftest import fixture

from newsroom.pipeline import ingest_observation
from newsroom.story.claim_validator import validate_script
from newsroom.story.context_builder import build_fact_context
from newsroom.story.script_generator import _script_from_template
from newsroom.story.story_planner import plan_story
from newsroom.storage import repositories as repo

EV_ID = "weather_alert:urn:oid:2.49.0.1.840.0.simnws1"


def test_nws_observation_creates_weather_alert_event(conn, nws):
    out = ingest_observation(conn, nws, fixture("nws_warning.json"),
                             "fixture://nws", "2026-11-07T15:05:00Z")
    assert out["new_event"] == 1
    assert out["new_revision"] == 1
    ev = repo.get_event(conn, EV_ID)
    assert ev is not None
    assert ev.event_type == "weather_alert"
    assert ev.impact_level == "BREAKING"       # 0.833 >= 0.60
    assert ev.title == "Tornado Warning — Central Valley"
    assert ev.latitude == 38.52 and ev.longitude == -121.44  # ring centroid
    assert ev.extra["alert_event"] == "Tornado Warning"
    facts = {f.field: f for f in repo.current_facts(conn, ev.event_id)}
    assert facts["alert_event"].value == "Tornado Warning"
    assert facts["severity_text"].value == "Severe"
    assert facts["certainty"].value == "Likely"
    assert facts["instruction"].value == "Take shelter now."
    assert facts["motion"].value == "TORNADO MOVING EAST AT 25 MPH"


def test_nws_revision_supersedes_facts_and_escalates(conn, nws):
    ingest_observation(conn, nws, fixture("nws_warning.json"),
                       "f", "2026-11-07T15:05:00Z")
    out = ingest_observation(conn, nws, fixture("nws_warning_rev2.json"),
                             "f", "2026-11-07T16:35:00Z")
    assert out["new_event"] == 0
    assert out["new_revision"] == 1
    ev = repo.get_event(conn, EV_ID)
    assert ev.current_revision == 2
    assert ev.impact_level == "FLASH"          # observed+immediate: 0.938

    hist = repo.facts_for_field(conn, ev.event_id, "certainty")
    assert [(f.value, f.status) for f in hist] == [
        ("Likely", "superseded"), ("Observed", "current")]
    revs = repo.get_revisions(conn, ev.event_id)
    assert "certainty" in revs[1].changed_fields
    assert "urgency" in revs[1].changed_fields
    assert "expires" in revs[1].changed_fields


def test_nws_template_script_validates(conn, nws, settings):
    ingest_observation(conn, nws, fixture("nws_warning.json"),
                       "f", "2026-11-07T15:05:00Z")
    ingest_observation(conn, nws, fixture("nws_warning_rev2.json"),
                       "f", "2026-11-07T16:35:00Z")
    ctx = build_fact_context(conn, EV_ID)
    plan = plan_story(conn, EV_ID)
    script = _script_from_template(ctx, plan, settings)
    ok, errors = validate_script(script, ctx, settings)
    assert ok, errors
    assert "Tornado Warning" in script.headline
    # Revision sentence reports the certainty upgrade, citing both facts.
    rev_seg = [s for s in script.segments if "initially reported" in s.text]
    assert rev_seg and len(rev_seg[0].fact_ids) == 2
    assert "Likely" in rev_seg[0].text and "Observed" in rev_seg[0].text
    # Every factual segment cites at least one fact.
    for seg in script.segments:
        if "developing story" not in seg.text.lower():
            assert seg.fact_ids


def test_different_alert_types_do_not_fuse(conn, nws):
    """A Flood Watch co-located with a Tornado Warning is a new event (§16)."""
    ingest_observation(conn, nws, fixture("nws_warning.json"),
                       "f", "2026-11-07T15:05:00Z")
    other = json.loads(fixture("nws_warning.json"))
    other["id"] = "urn:oid:2.49.0.1.840.0.simnws9"
    props = dict(other["properties"])
    props["event"] = "Flood Watch"
    props["severity"] = "Moderate"
    props["certainty"] = "Possible"
    props["urgency"] = "Future"
    props["headline"] = "Flood Watch issued November 7 by NWS Sacramento CA"
    other["properties"] = props
    out = ingest_observation(conn, nws, json.dumps(other), "f",
                             "2026-11-07T15:06:00Z")
    assert out["new_event"] == 1
    assert out["event_id"] != EV_ID
    assert out["correlation"] == "no match"


def test_keepalive_and_test_status_skipped(conn, nws):
    """NWS keep-alive heartbeats are evidence, not events."""
    keep = json.loads(fixture("nws_warning.json"))
    keep["id"] = "urn:oid:2.49.0.1.840.0-KEEPALIVE-1"
    props = dict(keep["properties"])
    props["status"] = "Test"
    props["event"] = "Test Message"
    keep["properties"] = props
    out = ingest_observation(conn, nws, json.dumps(keep), "f",
                             "2026-11-07T15:05:00Z")
    assert out["new_observation"] == 1          # evidence retained (§9)
    assert out["event_id"] is None
    assert repo.list_events(conn) == []


def test_same_alert_id_reobserved_is_revision_not_new_event(conn, nws):
    """NWS re-issues the same alert id with updates — one event, revisions."""
    ingest_observation(conn, nws, fixture("nws_warning.json"),
                       "f", "2026-11-07T15:05:00Z")
    out = ingest_observation(conn, nws, fixture("nws_warning_rev2.json"),
                             "f", "2026-11-07T16:35:00Z")
    assert out["event_id"] == EV_ID
    assert out["correlation"] == "source id match"
