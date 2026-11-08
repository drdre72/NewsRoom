"""Offline source contracts: stable identity, scoring and cited copy."""
import json

import pytest

from conftest import fixture
from newsroom.ingestion.adapters.coinbase import CoinbaseAdapter
from newsroom.ingestion.adapters.nhc import NhcAdapter
from newsroom.ingestion.adapters.treasury import TreasuryAdapter
from newsroom.pipeline import ingest_observation
from newsroom.storage import repositories as repo
from newsroom.story.claim_validator import validate_script
from newsroom.story.context_builder import build_fact_context
from newsroom.story.script_generator import _script_from_template
from newsroom.story.story_planner import plan_story


def _adapter(kind, name, config):
    return kind(name=name, config=config)


def _validate(conn, settings, event_id):
    ctx = build_fact_context(conn, event_id)
    script = _script_from_template(ctx, plan_story(conn, event_id), settings)
    ok, errors = validate_script(script, ctx, settings)
    assert ok, errors
    return script


def test_treasury_monthly_rate_is_its_own_citable_series(conn, settings):
    adapter = _adapter(TreasuryAdapter, "treasury", {})
    raw = fixture("treasury_rates.json")
    obs = adapter.normalize(raw)
    assert obs.fields["series_id"] == "Treasury_Notes"
    assert obs.fields["rate_value"] == "3.399"
    assert obs.fields["severity_norm"] == pytest.approx(0.184)
    result = ingest_observation(conn, adapter, raw, "fixture://treasury",
                                repo.utcnow())
    assert result["event_id"] == "economic_indicator:Treasury_Notes"
    script = _validate(conn, settings, result["event_id"])
    assert "3.399 percent" in script.segments[0].text
    assert "3.215 percent" in script.segments[1].text
    assert ingest_observation(conn, adapter, raw, "fixture://treasury",
                              repo.utcnow())["skipped"] == "duplicate payload"


def test_nhc_advisory_revises_storm_and_keeps_provenance(conn, settings):
    adapter = _adapter(NhcAdapter, "nhc", {})
    raw = fixture("nhc_storm.json")
    first = ingest_observation(conn, adapter, raw, "fixture://nhc",
                               repo.utcnow())
    assert first["event_id"] == "tropical_cyclone:cp042026"
    assert first["new_event"] == 1
    obs = adapter.normalize(raw)
    assert (obs.latitude, obs.longitude) == (11.5, -153.9)
    assert obs.fields["advisory_num"] == "005"
    updated = json.loads(raw)
    updated["publicAdvisory"]["advNum"] = "006"
    updated["intensity"] = "60"
    updated["lastUpdate"] = "2026-11-08T03:00:00.000Z"
    second = ingest_observation(conn, adapter, json.dumps(updated),
                                "fixture://nhc", repo.utcnow())
    assert second["event_id"] == first["event_id"]
    assert second["revision"] == first["revision"] + 1
    assert _validate(conn, settings, second["event_id"])


def test_nearby_named_storms_do_not_fuse(conn):
    adapter = _adapter(NhcAdapter, "nhc", {})
    first = json.loads(fixture("nhc_storm.json"))
    second = dict(first, id="cp052026", name="Koa",
                  latitudeNumeric=11.6, longitudeNumeric=-153.8)
    a = ingest_observation(conn, adapter, json.dumps(first),
                           "fixture://nhc", repo.utcnow())
    b = ingest_observation(conn, adapter, json.dumps(second),
                           "fixture://nhc", repo.utcnow())
    assert b["new_event"] == 1
    assert a["event_id"] != b["event_id"]


def test_market_movement_uses_previous_quote_and_not_cross_pair(conn, settings):
    adapter = _adapter(CoinbaseAdapter, "coinbase", {})
    raw = fixture("coinbase_btc.json")
    first = ingest_observation(conn, adapter, raw, "fixture://coinbase",
                               repo.utcnow())
    assert first["event_id"] == "market_quote:BTC-USD"
    assert first["impact"]["level"] in ("NORMAL", "BACKGROUND")
    second_raw = json.loads(raw)
    second_raw["data"]["amount"] = "79500.00"
    second = ingest_observation(conn, adapter, json.dumps(second_raw),
                                "fixture://coinbase", repo.utcnow())
    assert second["revision"] == 2
    assert second["impact"]["level"] in ("BREAKING", "UPDATE")
    fields = {f.field: f.value for f in repo.current_facts(
        conn, second["event_id"])}
    assert fields["previous_value"] == 75000
    assert fields["delta_pct"] == 6
    assert fields["severity_norm"] == 1
    script = _validate(conn, settings, second["event_id"])
    assert "79500.00 USD" in script.segments[0].text
    assert "75000.00 USD" in script.segments[1].text
    eth = json.loads(raw)
    eth.update(pair="ETH-USD", label="Ethereum")
    eth["data"].update(base="ETH", amount="2500.00")
    third = ingest_observation(conn, adapter, json.dumps(eth),
                               "fixture://coinbase", repo.utcnow())
    assert third["new_event"] == 1
    assert third["event_id"] == "market_quote:ETH-USD"
    assert _validate(conn, settings, third["event_id"])


def test_broadcast_asset_selection_ignores_longform_video(conn):
    from newsroom.models.event import CanonicalEvent
    event = CanonicalEvent(event_id="earthquake:asset", event_type="earthquake",
                           status="active", severity=5, confidence=0.95,
                           first_seen=repo.utcnow(), last_updated=repo.utcnow(),
                           current_revision=1)
    repo.upsert_event(conn, event)
    repo.insert_asset(conn, "short", "video", "/tmp/short.mp4", "hash",
                      event.event_id, 1, {})
    repo.insert_asset(conn, "long", "video", "/tmp/long.mp4", "hash",
                      event.event_id, 1, {"format": "longform"})
    assert repo.latest_video_asset(conn, event.event_id, 1)["path"] == \
        "/tmp/short.mp4"


def test_coinbase_fetch_failure_is_visible(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("source unavailable")

    monkeypatch.setattr("newsroom.ingestion.adapters.coinbase.fetch_bytes", fail)
    adapter = _adapter(CoinbaseAdapter, "coinbase",
                       {"pairs": {"BTC-USD": {"label": "Bitcoin"}}})
    with pytest.raises(OSError, match="source unavailable"):
        adapter.fetch()


def test_empty_feed_respects_poll_interval(conn, tmp_path, monkeypatch):
    from newsroom.config import Settings
    from newsroom.ingestion import polling
    from newsroom.ingestion.adapters.base import FetchResult

    class EmptyAdapter:
        def fetch(self, **kwargs):
            calls.append(1)
            return FetchResult(feed_url="fixture://empty",
                               retrieved_at=repo.utcnow(), payloads=[])

    calls = []
    settings = Settings({"sources": {
        "empty-test": {"kind": "empty", "enabled": True,
                       "poll_every_seconds": 3600}}}, tmp_path)
    monkeypatch.setattr(polling, "adapter_for",
                        lambda *args: EmptyAdapter())
    try:
        assert polling.poll_once(conn, settings, lambda *args: {})[
            "sources"]["empty-test"]["items"] == 0
        assert polling.poll_once(conn, settings, lambda *args: {})[
            "sources"]["empty-test"]["skipped"] == "interval"
        assert len(calls) == 1
    finally:
        polling._last_successful_poll.pop("empty-test", None)
