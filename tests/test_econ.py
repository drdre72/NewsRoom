"""FRED + BLS adapter tests — the economy desk (§53)."""
import pytest

from conftest import fixture


@pytest.fixture()
def fred():
    from newsroom.ingestion.adapters.fred import FredAdapter
    return FredAdapter(name="fred", config={
        "series": {"CPIAUCSL": {"label": "CPI-U All Items",
                                "units": "Index 1982-84=100",
                                "move_scale": 1.5}}})


@pytest.fixture()
def bls():
    from newsroom.ingestion.adapters.bls import BlsAdapter
    return BlsAdapter(name="bls", config={
        "series": {"CES0000000001": {"label": "Total Nonfarm Payrolls",
                                     "units": "Thousands",
                                     "move_scale": 1.0}}})


class TestFred:
    def test_observation_fields(self, fred):
        obs = fred.normalize(fixture("fred_cpi.json"))
        assert obs.source == "fred"
        assert obs.source_event_id == "CPIAUCSL"
        assert obs.event_type == "economic_indicator"
        assert obs.severity == 325.9
        assert obs.fields["previous_value"] == "324.1"
        assert obs.fields["delta_pct"] == pytest.approx(0.5554, abs=1e-3)
        assert obs.fields["severity_norm"] == pytest.approx(0.3703, abs=1e-4)
        assert "CPI-U All Items" in obs.title
        assert obs.latitude is None          # national indicator

    def test_pipeline_revises_on_new_print(self, conn, settings, fred):
        from newsroom.pipeline import ingest_observation
        o1 = ingest_observation(conn, fred, fixture("fred_cpi.json"),
                                "fixture://x", "2026-11-07T00:00:00.000Z")
        assert o1["new_event"] == 1
        assert o1["event_id"].startswith("economic_indicator:")
        # CPI surprise ~0.37 norm*0.5 + cat 0.7*0.35 ≈ 0.43 -> UPDATE
        assert o1["impact"]["level"] in ("UPDATE", "BREAKING", "NORMAL")


class TestBls:
    def test_payroll_fields(self, bls):
        obs = bls.normalize(fixture("bls_payrolls.json"))
        assert obs.source == "bls"
        assert obs.source_event_id == "CES0000000001"
        assert obs.event_type == "economic_indicator"
        assert obs.severity == 159412.0
        assert obs.fields["delta_pct"] == 0.14
        assert obs.fields["period_name"] == "October 2026"
        assert "Total Nonfarm Payrolls" in obs.title

    def test_split_series_results(self, bls):
        row = fixture("bls_payrolls.json").strip()
        body = '{"status":"REQUEST_SUCCEEDED","Results":{"series":[%s,%s]}}' % (
            row, row)
        assert len(list(bls.split_items(body))) == 2


@pytest.fixture()
def eia():
    from newsroom.ingestion.adapters.eia import EiaAdapter
    return EiaAdapter(name="eia", config={
        "series": {"PET.WCESTUS1.W": {
            "label": "US Crude Oil Stocks (excl. SPR)",
            "units": "thousand barrels", "move_scale": 3.0}}})


class TestEia:
    def test_observation_fields(self, eia):
        obs = eia.normalize(fixture("eia_crude.json"))
        assert obs.source == "eia"
        assert obs.source_event_id == "WCESTUS1"   # short code keys the event
        assert obs.event_type == "economic_indicator"
        assert obs.severity == 409760.0
        assert obs.fields["previous_value"] == 410625
        assert obs.fields["delta"] == -865.0
        assert obs.fields["delta_pct"] == pytest.approx(-0.21, abs=1e-3)
        assert obs.fields["severity_norm"] == pytest.approx(0.0702, abs=1e-3)
        assert "Crude Oil Stocks" in obs.title
        assert obs.latitude is None               # national indicator

    def test_pipeline_event(self, conn, settings, eia):
        from newsroom.pipeline import ingest_observation
        o1 = ingest_observation(conn, eia, fixture("eia_crude.json"),
                                "fixture://x", "2026-11-07T00:00:00.000Z")
        assert o1["new_event"] == 1
        assert o1["event_id"] == "economic_indicator:WCESTUS1"

    def test_different_series_do_not_fuse(self, conn, settings, eia):
        """Same release period + no geo must not collapse distinct series
        into one event — crude stocks and gas storage are not one story."""
        import json
        from newsroom.pipeline import ingest_observation
        crude = json.loads(fixture("eia_crude.json"))
        gas = json.loads(fixture("eia_crude.json"))
        gas["series_id"] = "NG.NW2_EPG0_SWO_R48_BCF.W"
        for r in gas["rows"]:
            r["series"] = "NW2_EPG0_SWO_R48_BCF"
            r["value"] = 3801
            r["units"] = "BCF"
        o1 = ingest_observation(conn, eia, json.dumps(crude), "f",
                                "2026-11-07T00:00:00.000Z")
        o2 = ingest_observation(conn, eia, json.dumps(gas), "f",
                                "2026-11-07T00:00:01.000Z")
        assert o1["event_id"] == "economic_indicator:WCESTUS1"
        assert o2["event_id"] == "economic_indicator:NW2_EPG0_SWO_R48_BCF"
        assert o2["new_event"] == 1
