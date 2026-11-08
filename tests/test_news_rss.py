"""News RSS adapter tests — attributed press reports (§53).

Key invariant: press reports attribute claims to outlets. They never
assert outlet claims as independently verified, and keyword scoring caps
below BREAKING so headlines alone can't interrupt programming.
"""
import json

from conftest import fixture

from newsroom.pipeline import ingest_observation
from newsroom.story.claim_validator import validate_script
from newsroom.story.context_builder import build_fact_context
from newsroom.story.script_generator import _script_from_template
from newsroom.story.story_planner import plan_story
from newsroom.storage import repositories as repo

import pytest


@pytest.fixture
def bbc():
    from newsroom.ingestion.adapters.news_rss import NewsRssAdapter
    return NewsRssAdapter(name="bbc", config={
        "authority": 0.75, "format": "rss", "outlet": "BBC World",
        "feeds": {"world": "fixture://bbc"}, "poll_feed": "world"})


@pytest.fixture
def omg():
    from newsroom.ingestion.adapters.news_rss import NewsRssAdapter
    return NewsRssAdapter(name="omg", config={
        "authority": 0.40, "format": "wp_json",
        "outlet": "O'Keefe Media Group",
        "feeds": {"posts": "fixture://omg"}, "poll_feed": "posts"})


def _seed(conn, adapter):
    body = adapter._rss_items(fixture("bbc_world.rss"))
    for item in body:
        ingest_observation(conn, adapter, item,
                           "fixture://bbc", "2026-11-07T15:00:00Z")


def test_rss_items_parse(bbc):
    items = bbc._rss_items(fixture("bbc_world.rss"))
    assert len(items) == 2                       # malformed item skipped
    first = json.loads(items[0])
    assert "Kharkiv" in first["title"]
    assert first["published"].startswith("2026-11-07")


def test_observation_attributed_and_scored(conn, bbc):
    _seed(conn, bbc)
    events = repo.list_events(conn, event_type="news_report", limit=10)
    assert len(events) == 2
    ev = max(events, key=lambda e: e.impact_score)
    assert ev.title.startswith("BBC World:")
    assert ev.impact_level in ("UPDATE", "NORMAL")   # never BREAKING
    facts = {f.field: f for f in repo.current_facts(conn, ev.event_id)}
    assert facts["outlet"].value == "BBC World"
    assert "Kharkiv" in facts["headline"].value
    assert facts["severity_norm"].value == 0.45      # "killed" keyword


def test_duplicate_article_dedupes(conn, bbc):
    _seed(conn, bbc)
    out2 = None
    for item in bbc._rss_items(fixture("bbc_world.rss")):
        out2 = ingest_observation(conn, bbc, item,
                                  "fixture://bbc", "2026-11-07T15:05:00Z")
    assert out2["new_event"] == 0


def test_news_template_attributes_and_validates(conn, bbc, settings):
    _seed(conn, bbc)
    events = repo.list_events(conn, event_type="news_report", limit=10)
    eid = max(events, key=lambda e: e.impact_score).event_id
    ctx = build_fact_context(conn, eid)
    script = _script_from_template(ctx, plan_story(conn, eid), settings)
    ok, errors = validate_script(script, ctx, settings)
    assert ok, errors
    assert any("BBC World reports" in s.text for s in script.segments)
    # Attribution is the first claim — no segment asserts the claim itself
    assert "killed" in script.segments[0].text
    assert script.segments[0].fact_ids


def test_wp_json_items(omg):
    posts = json.dumps([{
        "id": 5982, "date_gmt": "2026-11-05T17:47:42",
        "link": "https://okeefemediagroup.com/story/",
        "title": {"rendered": "UNDERCOVER: Agency insider reveals fraud"},
        "excerpt": {"rendered": "<p>Exclusive footage.</p>"}}])
    items = omg._wp_items(posts)
    assert len(items) == 1
    obs = omg.normalize(items[0])
    assert obs.event_type == "news_report"
    assert obs.title.startswith("O'Keefe Media Group:")
    assert obs.fields["severity_norm"] == 0.25      # no keyword boost


def test_atom_feed_parsing(bbc):
    atom = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Atom test headline</title>
        <link href="https://example.com/a1" rel="alternate"/>
        <id>urn:atom:1</id>
        <published>2026-11-07T10:00:00Z</published>
        <summary>An atom item.</summary>
      </entry>
    </feed>"""
    items = bbc._rss_items(atom)
    assert len(items) == 1
    assert json.loads(items[0])["link"] == "https://example.com/a1"
