"""Crime desk adapter — phrase scoring, event type, registration."""
import json

from newsroom.ingestion.adapters import adapter_for
from newsroom.ingestion.adapters.crime_rss import CrimeRssAdapter
from newsroom.pipeline import ingest_observation


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


def _item(title: str, guid: str, published: str) -> str:
    return json.dumps({"title": title, "link": f"https://x/{guid}",
                       "published": published, "summary": "", "guid": guid})


class TestPressStoryFusion:
    """Same-story coverage across outlets must merge into one event —
    rolling coverage is one event with revisions, not 40 airings (§16)."""

    def test_same_story_hours_later_fuses(self, conn, settings):
        a = _adapter()
        first = ingest_observation(
            conn, a, _item("Mass shooting in Krakow leaves six dead", "a1",
                           "2026-11-09T10:00:00Z"), "f", "2026-11-09T10:00:00Z")
        # Same incident three hours later, wire-copy headline phrasing.
        second = ingest_observation(
            conn, a, _item("Krakow mass shooting leaves six dead, mayor "
                           "confirms", "a2", "2026-11-09T13:00:00Z"),
            "f", "2026-11-09T13:00:00Z")
        assert first["new_event"] == 1
        assert second["new_event"] == 0
        assert second["event_id"] == first["event_id"]
        assert "headline" in second["correlation"]

    def test_different_city_stays_separate(self, conn, settings):
        a = _adapter()
        ingest_observation(
            conn, a, _item("Mass shooting in Krakow leaves six dead", "b1",
                           "2026-11-09T10:00:00Z"), "f", "2026-11-09T10:00:00Z")
        out = ingest_observation(
            conn, a, _item("Mass shooting in Denver kills two", "b2",
                           "2026-11-09T11:00:00Z"), "f", "2026-11-09T11:00:00Z")
        assert out["new_event"] == 1     # different city tokens → new event

    def test_angle_story_not_merged(self, conn, settings):
        a = _adapter()
        ingest_observation(
            conn, a, _item("Mass shooting in Krakow leaves six dead", "c1",
                           "2026-11-09T10:00:00Z"), "f", "2026-11-09T10:00:00Z")
        out = ingest_observation(
            conn, a, _item("Pope mourns victims of Krakow massacre", "c2",
                           "2026-11-09T14:00:00Z"), "f", "2026-11-09T14:00:00Z")
        assert out["new_event"] == 1     # different angle → its own event
