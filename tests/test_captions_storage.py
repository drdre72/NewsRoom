"""Captions, storage integrity, and gate tests (offline)."""
import json

from conftest import fixture

from newsroom.editor.captions import write_ass, write_srt
from newsroom.pipeline import ingest_observation
from newsroom.quality.stale_revision import check_stale
from newsroom.storage.database import integrity_check


def test_ass_wraps_long_lines(tmp_path):
    items = [(0.0, 4.0, "A very long caption line that must wrap inside the "
                       "safe zone so it never bleeds off the vertical frame.")]
    out = write_ass(items, tmp_path / "c.ass", 1080, 1920)
    text = out.read_text()
    assert "\\N" in text                       # manual line break present
    assert "MarginV" in text
    assert "WrapStyle: 0" in text


def test_srt_format(tmp_path):
    out = write_srt([(1.5, 4.25, "hello world")], tmp_path / "c.srt")
    body = out.read_text()
    assert "00:00:01,500 --> 00:00:04,250" in body
    assert "hello world" in body


def test_integrity_check_clean_db(conn):
    status = integrity_check(conn)
    assert status["integrity"] == "ok"
    assert status["wal"] is True
    assert status["schema_version"] >= 1


def test_stale_revision_gate(conn, usgs):
    ingest_observation(conn, usgs, fixture("earthquake_rev1.geojson"),
                       "f", "2026-11-07T00:00:00Z")
    ingest_observation(conn, usgs, fixture("earthquake_rev2.geojson"),
                       "f", "2026-11-07T00:05:00Z")
    manifest = {"event_id": "earthquake:us7000sim2", "event_revision": 1}
    result = check_stale(conn, manifest)
    assert result["ok"] is False
    assert result["current_revision"] == 2
    fresh = check_stale(conn, {"event_id": "earthquake:us7000sim2",
                               "event_revision": 2})
    assert fresh["ok"] is True
