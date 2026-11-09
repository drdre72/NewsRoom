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
    assert obs.fields["category"] == "Google News"  # fallback when no <source>
    assert obs.fields["outlet"] == "Google News"
    assert obs.source == "gnews_crime"


def test_original_source_credited_not_google_news():
    """Google News RSS carries <source> — credit Reuters/AP first, with
    'via Google News' at the end, and keep the Google News link."""
    a = _adapter()
    payload = json.dumps({
        "title": "Krakow mass shooter dies as dispute builds",
        "link": "https://news.google.com/rss/articles/CBMiabc",
        "published": "2026-11-09T06:38:00Z",
        "summary": "", "guid": "CBMiabc",
        "source": "Reuters"})
    obs = a.normalize(payload)
    assert obs.fields["outlet"] == "Reuters"
    assert obs.title.startswith("Reuters: ")
    assert "(via Google News)" in obs.title
    assert "Google News" not in obs.title.split(":")[0]  # not the lead
    assert obs.fields["report_url"] == "https://news.google.com/rss/articles/CBMiabc"
    assert obs.fields.get("via") == "Google News"


def test_source_suffix_stripped_from_title():
    """Google News titles end with ' - Source' — _rss_items strips it
    since we carry the source separately via <source>."""
    a = _adapter()
    # Simulate what _rss_items produces after stripping
    payload = json.dumps({
        "title": "Krakow mass shooter dies",
        "link": "https://x", "published": "2026-11-09T06:38:00Z",
        "summary": "", "guid": "g1", "source": "Reuters"})
    obs = a.normalize(payload)
    assert obs.fields["headline"] == "Krakow mass shooter dies"
    assert obs.fields["outlet"] == "Reuters"
    assert "(via Google News)" in obs.title


def test_direct_feed_no_via():
    """Direct feeds (BBC, Al Jazeera) don't get 'via Google News'."""
    a = _adapter()
    payload = json.dumps({
        "title": "BBC: Floods in Bangladesh",
        "link": "https://bbc.co.uk/x", "published": "2026-11-09T06:38:00Z",
        "summary": "", "guid": "g2", "source": ""})
    obs = a.normalize(payload)
    assert "via Google News" not in obs.title
    assert obs.fields.get("via") == ""


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
