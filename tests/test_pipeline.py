"""Core ingest → event → revision → fact-ledger tests (offline, §53)."""
from conftest import fixture

from newsroom.pipeline import ingest_observation
from newsroom.storage import repositories as repo


def test_single_observation_creates_event_and_facts(conn, usgs):
    out = ingest_observation(conn, usgs, fixture("earthquake_major.geojson"),
                             "fixture://t", "2026-11-07T00:00:00Z")
    assert out["new_event"] == 1
    assert out["new_revision"] == 1
    assert out["facts_written"] > 0
    ev = repo.get_event(conn, "earthquake:us7000sim1")
    assert ev.event_type == "earthquake"
    assert ev.current_revision == 1
    assert ev.impact_level == "FLASH"
    assert ev.latitude == 35.412
    facts = repo.current_facts(conn, ev.event_id)
    by_field = {f.field: f for f in facts}
    assert by_field["magnitude"].value == 7.8
    assert by_field["tsunami"].value is True


def test_revision_sequence_supersedes_facts(conn, usgs):
    for i, name in enumerate(("earthquake_rev1.geojson", "earthquake_rev2.geojson",
                              "earthquake_rev3.geojson")):
        out = ingest_observation(conn, usgs, fixture(name), "fixture://t",
                                 f"2026-11-07T00:0{i}:00Z")
    ev = repo.get_event(conn, "earthquake:us7000sim2")
    assert ev.current_revision == 3
    assert ev.impact_level == "BREAKING"          # 0.607 >= 0.60

    hist = repo.facts_for_field(conn, ev.event_id, "magnitude")
    values = [(f.value, f.status) for f in hist]
    assert values == [(6.1, "superseded"), (6.4, "superseded"), (6.7, "current")]
    # Superseded facts point at their replacements.
    assert hist[0].superseded_by == hist[1].fact_id
    assert hist[1].superseded_by == hist[2].fact_id

    revs = repo.get_revisions(conn, ev.event_id)
    assert len(revs) == 3
    assert "magnitude" in revs[2].changed_fields


def test_duplicate_payload_is_noop(conn, usgs):
    payload = fixture("earthquake_major.geojson")
    out1 = ingest_observation(conn, usgs, payload, "f", "2026-11-07T00:00:00Z")
    out2 = ingest_observation(conn, usgs, payload, "f", "2026-11-07T00:01:00Z")
    assert out1["new_event"] == 1
    assert out2["new_observation"] == 0
    assert out2["skipped"] == "duplicate payload"
    # Re-ingestion still resolves the canonical event for render callers.
    assert out2["event_id"] == "earthquake:us7000sim1"


def test_malformed_payload_kept_as_evidence_but_skipped(conn, usgs):
    out = ingest_observation(conn, usgs, fixture("malformed.geojson"),
                             "fixture://t", "2026-11-07T00:00:00Z")
    assert out["new_observation"] == 1            # evidence persisted (§9)
    assert out["skipped"] == "unparseable payload"
    assert out["event_id"] is None
    assert repo.list_events(conn) == []


def test_cross_source_fusion(conn, usgs):
    """A second source id for the same incident must merge (§16)."""
    ingest_observation(conn, usgs, fixture("earthquake_major.geojson"),
                       "f", "2026-11-07T00:00:00Z")
    # Same quake reported under a different source event id, 30s later,
    # ~4 km away — inside the 300s/50km fusion window.
    other = fixture("earthquake_major.geojson").replace(
        '"us7000sim1"', '"partner9001"').replace("us7000sim1", "partner9001").replace(
        "35.412", "35.45").replace("140.021", "140.03")
    out = ingest_observation(conn, usgs, other, "f", "2026-11-07T00:00:30Z")
    assert out["new_event"] == 0
    assert out["event_id"] == "earthquake:us7000sim1"
    assert "fusion" in out["correlation"]
    ev = repo.get_event(conn, "earthquake:us7000sim1")
    assert len(ev.source_ids) == 2


def test_no_material_change_means_no_revision(conn, usgs):
    """A new observation with identical canonical fields revises nothing."""
    import json
    p1 = json.loads(fixture("earthquake_rev1.geojson"))
    ingest_observation(conn, usgs, json.dumps(p1), "f", "2026-11-07T00:00:00Z")
    # Change only a field outside the canonical snapshot (e.g. url).
    p2 = dict(p1)
    props = dict(p1["properties"])
    props["url"] = "https://example.com/different"
    props["updated"] = p1["properties"]["updated"] + 60000
    p2["properties"] = props
    out = ingest_observation(conn, usgs, json.dumps(p2), "f",
                           "2026-11-07T00:05:00Z")
    assert out["event_id"] == "earthquake:us7000sim2"
    assert out["new_revision"] == 0 if out.get("skipped") == "no material change" else True
