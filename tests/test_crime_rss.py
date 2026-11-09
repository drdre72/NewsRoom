"""Crime desk adapter — phrase scoring, event type, registration."""
import json

from newsroom.ingestion.adapters import adapter_for
from newsroom.ingestion.adapters.crime_rss import CrimeRssAdapter


def _adapter(**extra):
    cfg = {"format": "rss", "outlet": "Google News",
           "feeds": {"crime": "https://x/rss"}, "poll_feed": "crime",
           **extra}
    return CrimeRssAdapter(name="gnews_crime", config=cfg)


def test_registered_kind():
    a = adapter_for("crime_rss", "gnews_crime",
                    {"feeds": {"x": "https://x"}})
    assert isinstance(a, CrimeRssAdapter)
    assert a.event_type == "crime_report"


def test_phrase_scoring_beats_single_words():
    a = _adapter()
    assert a._snorm("Manhunt underway after mass shooting in mall") >= 0.75
    assert a._snorm("Amber Alert issued for abducted child") >= 0.65
    assert a._snorm("Two charged in armed robbery") >= 0.45
    assert a._snorm("City council meets Tuesday") == 0.30  # baseline floor


def test_normalize_emits_crime_report():
    a = _adapter()
    payload = json.dumps({
        "title": "FBI arrests suspect in bombing plot",
        "link": "https://example.com/x",
        "published": "2026-01-01T00:00:00Z",
        "summary": "details", "guid": "g1"})
    obs = a.normalize(payload)
    assert obs and obs.event_type == "crime_report"
    assert obs.severity >= 0.60            # bombing + fbi + arrested
    assert obs.fields["category"] == "Google News"
    assert obs.fields["outlet"] == "Google News"
    assert obs.source == "gnews_crime"
