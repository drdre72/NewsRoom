"""FEMA + CISA KEV adapter tests — the U.S. desk."""
import json

import pytest

from conftest import fixture


@pytest.fixture()
def fema():
    from newsroom.ingestion.adapters.fema import FemaAdapter
    return FemaAdapter(name="fema", config={"top": 60})


@pytest.fixture()
def cisa():
    from newsroom.ingestion.adapters.cisa import CisaAdapter
    return CisaAdapter(name="cisa", config={"lookback_days": 30})


class TestFema:
    def test_observation_fields(self, fema):
        obs = fema.normalize(fixture("fema_declaration.json"))
        assert obs.source == "fema"
        assert obs.source_event_id == "DR-4955-FL"
        assert obs.event_type == "disaster_declaration"
        assert obs.fields["declaration_label"] == "Major Disaster Declaration"
        assert obs.fields["incident_type"] == "Hurricane"
        assert obs.fields["state"] == "FL"
        assert obs.fields["area_count"] == 2
        assert "Bay (County)" in obs.fields["areas"]
        assert obs.fields["severity_norm"] == 1.0    # DR = major disaster
        assert "Hurricane Isaias" in obs.title

    def test_pipeline_scores_breaking(self, conn, settings, fema):
        from newsroom.pipeline import ingest_observation
        out = ingest_observation(conn, fema, fixture("fema_declaration.json"),
                                 "fixture://x", "2026-11-07T00:00:00.000Z")
        assert out["new_event"] == 1
        assert out["event_id"] == "disaster_declaration:DR-4955-FL"
        # DR hurricane: 0.55*1.0 + 0.30*0.9 = 0.82 -> BREAKING
        assert out["impact"]["level"] in ("BREAKING", "FLASH")

    def test_fm_declarations_score_lower(self, conn, settings, fema):
        from newsroom.pipeline import ingest_observation
        fm = json.loads(fixture("fema_declaration.json"))
        fm["declaration"] = "FM-5544-CA"
        fm["femaDeclarationString"] = "FM-5544-CA"
        fm["declarationType"] = "FM"
        fm["incidentType"] = "Fire"
        out = ingest_observation(conn, fema, json.dumps(fm),
                                 "fixture://y", "2026-11-07T00:00:00.000Z")
        # FM fire: 0.55*0.4 + 0.30*0.6 = 0.40 -> UPDATE, not BREAKING
        assert out["impact"]["level"] == "UPDATE"


class TestCisa:
    def test_observation_fields(self, cisa):
        obs = cisa.normalize(fixture("cisa_kev.json"))
        assert obs.source == "cisa_kev"
        assert obs.source_event_id == "CVE-2026-19868"
        assert obs.event_type == "cyber_advisory"
        assert obs.fields["vendor"] == "SonicWall"
        assert obs.fields["product"] == "SonicOS SSLVPN"
        assert obs.fields["due_date"] == "2026-11-20"
        assert obs.fields["ransomware_use"] == "Unknown"
        assert obs.fields["category"] == "actively exploited"
        assert obs.fields["severity_norm"] == 0.55
        assert "CVE-2026-19868" in obs.title

    def test_ransomware_boosts_severity(self, cisa):
        doc = json.loads(fixture("cisa_kev.json"))
        doc["knownRansomwareCampaignUse"] = "Known"
        obs = cisa.normalize(json.dumps(doc))
        assert obs.fields["severity_norm"] == pytest.approx(0.85, abs=0.01)
        assert obs.fields["category"] == "known exploited"

    def test_pipeline_event(self, conn, settings, cisa):
        from newsroom.pipeline import ingest_observation
        out = ingest_observation(conn, cisa, fixture("cisa_kev.json"),
                                 "fixture://x", "2026-11-07T00:00:00.000Z")
        assert out["new_event"] == 1
        assert out["event_id"] == "cyber_advisory:CVE-2026-19868"

    def test_missing_cve_dropped(self, cisa):
        assert cisa.normalize('{"vulnerabilityName": "x"}') is None
