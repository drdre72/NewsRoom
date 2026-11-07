"""EONET adapter tests (§53) — third event family, no pipeline rewrites."""
import json

import pytest

from conftest import fixture


@pytest.fixture()
def eonet():
    from newsroom.ingestion.adapters.eonet import EonetAdapter
    return EonetAdapter(name="eonet", config={})


class TestEonetNormalize:
    def test_wildfire_fields(self, eonet):
        obs = eonet.normalize(fixture("eonet_wildfire.json"))
        assert obs.source == "eonet"
        assert obs.source_event_id == "EONET_9001"
        assert obs.event_type == "wildfire"
        assert obs.latitude == pytest.approx(39.22)
        assert obs.longitude == pytest.approx(-120.15)
        assert obs.severity == 42500.0
        assert obs.status == "active"
        assert obs.location_label == "Pine Ridge, California"
        assert obs.fields["category"] == "wildfires"
        assert obs.fields["magnitude_unit"] == "acres"
        assert obs.fields["source_url"].startswith("https://inciweb")

    def test_closed_event_status(self, eonet):
        ev = json.loads(fixture("eonet_wildfire.json"))
        ev["closed"] = "2026-11-08T00:00:00Z"
        obs = eonet.normalize(json.dumps(ev))
        assert obs.status == "closed"
        assert obs.fields["closed"] is True
        assert obs.fields["closed_at"].startswith("2026-11-08")

    def test_skipped_categories_return_none(self, eonet):
        ev = json.loads(fixture("eonet_wildfire.json"))
        for cat in ("earthquakes", "waterColor", "dustHaze", "manmade"):
            ev["categories"] = [{"id": cat, "title": cat}]
            assert eonet.normalize(json.dumps(ev)) is None, cat

    def test_unknown_category_returns_none(self, eonet):
        ev = json.loads(fixture("eonet_wildfire.json"))
        ev["categories"] = [{"id": "somethingNew", "title": "New"}]
        assert eonet.normalize(json.dumps(ev)) is None

    def test_latest_geometry_wins(self, eonet):
        ev = json.loads(fixture("eonet_wildfire.json"))
        ev["geometry"].insert(0, {
            "magnitudeValue": 100.0, "magnitudeUnit": "acres",
            "date": "2026-11-05T00:00:00Z", "type": "Point",
            "coordinates": [-1.0, -1.0]})
        obs = eonet.normalize(json.dumps(ev))
        assert obs.severity == 42500.0        # latest entry, not the first
        assert obs.latitude == pytest.approx(39.22)

    def test_missing_id_skipped(self, eonet):
        assert eonet.normalize("{}") is None


class TestEonetSplit:
    def test_feed_splits_per_event(self, eonet):
        a = json.loads(fixture("eonet_wildfire.json"))
        b = json.loads(fixture("eonet_wildfire_rev2.json"))
        body = json.dumps({"title": "EONET Events", "events": [a, b]})
        assert len(list(eonet.split_items(body))) == 2


class TestEonetPipeline:
    def test_event_type_scores_and_revises(self, conn, settings, eonet):
        from newsroom.pipeline import ingest_observation
        out1 = ingest_observation(conn, eonet, fixture("eonet_wildfire.json"),
                                  "fixture://x", "2026-11-07T12:00:00.000Z")
        assert out1["new_event"] == 1
        assert out1["event_id"].startswith("wildfire:")
        # 42,500 acres -> severity norm ~0.085*0.45 + cat 0.75*0.35 ≈ 0.30 NORMAL
        assert out1["impact"]["level"] in ("NORMAL", "UPDATE")
        out2 = ingest_observation(conn, eonet,
                                  fixture("eonet_wildfire_rev2.json"),
                                  "fixture://x", "2026-11-07T18:00:00.000Z")
        assert out2["new_revision"] == 1
        assert out2["event_id"] == out1["event_id"]
        # severity superseded: 42500 -> 61200
        from newsroom.story.newsroom_memory import what_changed
        changed = {c["field"] for c in what_changed(conn, out1["event_id"])}
        assert "severity" in changed
