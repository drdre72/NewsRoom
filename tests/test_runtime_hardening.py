"""Offline regressions for rendering and editorial trust boundaries."""
from types import SimpleNamespace
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest

from conftest import fixture
from newsroom.models.story import Script, ScriptSegment
from newsroom.pipeline import ingest_observation
from newsroom.story.claim_validator import validate_script
from newsroom.story.context_builder import build_fact_context


def _earthquake_context(conn, usgs):
    result = ingest_observation(
        conn, usgs, fixture("earthquake_major.geojson"),
        "fixture://usgs", "2026-11-07T00:00:00Z")
    return build_fact_context(conn, result["event_id"])


def test_uncited_background_marker_does_not_waive_citations(conn, usgs, settings):
    ctx = _earthquake_context(conn, usgs)
    script = Script(headline="x", generator="longform:template",
                    event_id=ctx.event_id, revision=ctx.revision,
                    segments=[ScriptSegment(
                        "Earthquakes occur when stress is released. "
                        "This quake destroyed the city.", "", [])])
    ok, errors = validate_script(script, ctx, settings)
    assert not ok
    assert any("no valid fact citations" in error for error in errors)


def test_llm_cannot_use_template_citation_exemption(conn, usgs, settings):
    from newsroom.story.script_generator import _DEVELOPING_CLOSE

    ctx = _earthquake_context(conn, usgs)
    script = Script(headline="x", generator="ollama:test",
                    event_id=ctx.event_id, revision=ctx.revision,
                    segments=[ScriptSegment(_DEVELOPING_CLOSE, "", [])])
    ok, errors = validate_script(script, ctx, settings)
    assert not ok and any("no valid fact citations" in e for e in errors)


def test_claim_numbers_must_be_supported_by_cited_fact(conn, usgs, settings):
    ctx = _earthquake_context(conn, usgs)
    mag = next(f for f in ctx.current if f.field == "magnitude")
    depth = next(f for f in ctx.current if f.field == "depth_km")
    script = Script(headline="x", generator="ollama:test",
                    event_id=ctx.event_id, revision=ctx.revision,
                    segments=[ScriptSegment(
                        f"Magnitude {mag.value_display}; "
                        f"depth {depth.value_display}.", "", [mag.fact_id])])
    ok, errors = validate_script(script, ctx, settings)
    assert not ok and any("untraceable number" in e for e in errors)


def test_offline_map_waits_for_style_before_adding_sources():
    from newsroom.visuals.maps.renderer import MapLibreRenderer, _WEB_DIR

    renderer = MapLibreRenderer()
    available, reason = renderer.available()
    if not available:
        pytest.skip(reason)
    from playwright.sync_api import sync_playwright

    plan = SimpleNamespace(width=320, height=220)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 320, "height": 220})
            page.goto((_WEB_DIR / "map.html").as_uri())
            page.wait_for_function("window.__ready")
            page.evaluate("(p) => window.__initMap(p)",
                          renderer._map_config(plan, {}))
            assert page.evaluate("window.__map.isStyleLoaded()")
            page.evaluate("(p) => window.__setShot(p)",
                          '{"lat": 38.5, "lon": -121.4, "rings_km": [100]}')
            page.wait_for_function("window.__idle")
        finally:
            browser.close()


def test_loopback_preview_only_serves_public_files(tmp_path, settings):
    from newsroom.broadcast.preview import _Handler, _INDEX

    (tmp_path / "live.m3u8").write_text("#EXTM3U\n")
    (tmp_path / "state.json").write_text('{"on_air":"<img src=x onerror=alert(1)>"}')
    (tmp_path / "secret.txt").write_text("private")
    handler = partial(_Handler, directory=str(tmp_path), settings=settings)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        http = HTTPConnection("127.0.0.1", server.server_port)
        http.request("GET", "/secret.txt")
        response = http.getresponse()
        assert response.status == 404
        response.read()
        http.request("HEAD", "/secret.txt")
        response = http.getresponse()
        assert response.status == 404
        response.read()
        http.request("GET", "/live.m3u8")
        response = http.getresponse()
        assert response.status == 200
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        response.read()
        http.request("GET", "/", headers={"Host": "attacker.example"})
        response = http.getresponse()
        assert response.status == 403
        response.read()
        http.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert "innerHTML" not in _INDEX
    assert "textContent=j.state" in _INDEX
