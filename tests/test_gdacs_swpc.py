"""GDACS + SWPC adapter tests — sources 4 and 5 (§53)."""
import json

import pytest

from conftest import fixture


@pytest.fixture()
def gdacs():
    from newsroom.ingestion.adapters.gdacs import GdacsAdapter
    return GdacsAdapter(name="gdacs", config={})


@pytest.fixture()
def swpc():
    from newsroom.ingestion.adapters.swpc import SwpcAdapter
    return SwpcAdapter(name="swpc", config={})


class TestGdacs:
    def test_cyclone_fields(self, gdacs):
        obs = gdacs.normalize(fixture("gdacs_cyclone.xml"))
        assert obs.source == "gdacs"
        assert obs.source_event_id == "1002999.4"     # eventid.episodeid
        assert obs.event_type == "tropical_cyclone"
        assert obs.latitude == pytest.approx(14.2)
        assert obs.longitude == pytest.approx(122.5)
        assert obs.severity == 150.0
        assert obs.fields["alert_level"] == "Red"
        assert obs.fields["country"] == "Philippines"
        assert obs.fields["magnitude_unit"] == "km/h"
        assert obs.fields["category_title"] == "Tropical Cyclone"

    def test_rss_feed_splits(self, gdacs):
        items = list(gdacs.split_items(fixture("gdacs_rss.xml")))
        assert len(items) == 2
        types = {gdacs.normalize(i).event_type for i in items}
        assert types == {"tropical_cyclone", "flood"}

    def test_red_alert_scores_high(self, conn, settings, gdacs):
        from newsroom.pipeline import ingest_observation
        out = ingest_observation(conn, gdacs, fixture("gdacs_cyclone.xml"),
                                 "fixture://x", "2026-11-06T14:00:00.000Z")
        assert out["new_event"] == 1
        # Red alert + 150 km/h -> BREAKING or better
        assert out["impact"]["level"] in ("BREAKING", "FLASH")


class TestSwpc:
    def test_kindex_alert_fields(self, swpc):
        obs = swpc.normalize(fixture("swpc_alert_k7a.json"))
        assert obs.source == "swpc"
        assert obs.event_type == "space_weather"
        assert obs.source_event_id == "ALTK07-k7"
        assert obs.severity == 7.0
        assert obs.fields["noaa_scale"] == "G3"
        assert obs.fields["swpc_code"] == "ALTK07"
        assert "Geomagnetic K-index of 7" in obs.title
        assert obs.latitude is None          # space weather has no coords

    def test_feed_splits(self, swpc):
        items = list(swpc.split_items(fixture("swpc_alerts.json")))
        assert len(items) == 3
        ids = {swpc.normalize(i).source_event_id for i in items}
        assert ids == {"ALTK07-k7", "WARK06-k6"}

    def test_same_code_revises_not_duplicates(self, conn, settings, swpc):
        from newsroom.pipeline import ingest_observation
        o1 = ingest_observation(conn, swpc, fixture("swpc_alert_k7a.json"),
                                "fixture://x", "2026-11-07T14:00:00.000Z")
        o2 = ingest_observation(conn, swpc, fixture("swpc_alert_k7b.json"),
                                "fixture://x", "2026-11-07T15:00:00.000Z")
        assert o1["event_id"] == o2["event_id"]
        assert o2["new_revision"] == 1
        assert o1["event_id"].startswith("space_weather:")
        # K7 -> norm (7-3)/(9-3)=0.67*0.5 + cat 0.105 + alert g3 0.7*0.2
        #     ~= 0.58 -> UPDATE/NORMAL boundary; just assert it's scored
        assert o1["impact"]["score"] > 0.15
