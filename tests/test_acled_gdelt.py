"""ACLED + GDELT adapter tests — sources 6 and 7 (§53)."""
import json

import pytest

from conftest import fixture


@pytest.fixture()
def acled():
    from newsroom.ingestion.adapters.acled import AcledAdapter
    return AcledAdapter(name="acled", config={})


@pytest.fixture()
def gdelt():
    from newsroom.ingestion.adapters.gdelt import GdeltAdapter
    return GdeltAdapter(name="gdelt", config={})


class TestAcled:
    def test_battle_fields(self, acled):
        obs = acled.normalize(fixture("acled_battle.json"))
        assert obs.source == "acled"
        assert obs.source_event_id == "UKR12346"
        assert obs.event_type == "conflict_event"
        assert obs.latitude == pytest.approx(48.9)
        assert obs.longitude == pytest.approx(38.2)
        assert obs.severity == 12.0
        assert obs.fields["severity_norm"] == pytest.approx(0.24)
        assert obs.fields["actor1"] == "Military Forces of Country A"
        assert obs.fields["sub_event_type"] == "Armed clash"
        assert obs.fields["magnitude_unit"] == "fatalities"
        assert "Border District" in obs.title

    def test_protest_type_mapping(self, acled):
        row = json.loads(fixture("acled_battle.json"))
        row["event_type"] = "Protests"
        row["sub_event_type"] = "Peaceful protest"
        obs = acled.normalize(json.dumps(row))
        assert obs.event_type == "protest"
        assert obs.fields["category_title"] == "Protest"

    def test_split_items(self, acled):
        row = fixture("acled_battle.json").strip()
        body = json.dumps({"status": 1, "count": 2,
                           "data": [json.loads(row), json.loads(row)]})
        assert len(list(acled.split_items(body))) == 2

    def test_pipeline_scores(self, conn, settings, acled):
        from newsroom.pipeline import ingest_observation
        out = ingest_observation(conn, acled, fixture("acled_battle.json"),
                                 "fixture://x", "2026-11-06T12:00:00.000Z")
        assert out["new_event"] == 1
        assert out["event_id"].startswith("conflict_event:")
        # severity_norm 0.24*0.5 + battles 0.8*0.35 ≈ 0.40 -> UPDATE
        assert out["impact"]["level"] in ("UPDATE", "BREAKING")


class TestGdelt:
    def test_row_fields(self, gdelt):
        cols = fixture("gdelt_conflict.tsv").strip().split("\t")
        obs = gdelt.normalize(json.dumps(cols))
        assert obs.source == "gdelt"
        assert obs.source_event_id == "G991001235"
        assert obs.event_type == "conflict_event"
        assert obs.severity == 137.0
        assert obs.fields["goldstein"] == -8.5
        assert obs.fields["num_sources"] == "14"
        assert obs.fields["cameo_desc"] == "Assault"
        assert obs.fields["quad_class"] == "4"
        assert obs.latitude == pytest.approx(50.4)
        assert obs.fields["severity_norm"] == pytest.approx(137 / 150)

    def test_split_filters(self, gdelt):
        row = fixture("gdelt_conflict.tsv").strip()
        coop = row.split("\t"); coop[28] = "1"        # verbal cooperation
        quiet = row.split("\t"); quiet[31] = "1"      # num_sources < min
        body = "\n".join([row, "\t".join(coop), "\t".join(quiet)])
        items = list(gdelt.split_items(body))
        assert len(items) == 1                        # only the qualifying row

    def test_pipeline_scores(self, conn, settings, gdelt):
        from newsroom.pipeline import ingest_observation
        cols = fixture("gdelt_conflict.tsv").strip().split("\t")
        out = ingest_observation(conn, gdelt, json.dumps(cols),
                                 "fixture://x", "2026-11-06T15:30:00.000Z")
        assert out["new_event"] == 1
        # severity_norm 0.913*0.5 + material conflict 0.6*0.35 ≈ 0.67 -> BREAKING
        assert out["impact"]["level"] in ("BREAKING", "UPDATE")
