"""Official press releases must remain attributed and recent."""
import json
from datetime import datetime, timedelta, timezone

from newsroom.ingestion.adapters.doj_press import DojPressAdapter
from newsroom.pipeline import ingest_observation
from newsroom.storage import repositories as repo


def test_doj_press_filters_stale_and_untrusted_links(conn, monkeypatch):
    import newsroom.ingestion.adapters.doj_press as module

    now = datetime.now(timezone.utc)
    adapter = DojPressAdapter("doj_press", {
        "authority": 0.85, "outlet": "U.S. Department of Justice",
        "feeds": {"releases": "https://www.justice.gov/api/v1/press_releases.json"},
        "poll_feed": "releases"})

    def entry(uuid, age, url):
        return {"uuid": uuid, "date": str(int((now - timedelta(hours=age)).timestamp())),
                "title": "Suspect charged in robbery", "url": url}

    monkeypatch.setattr(module, "fetch_bytes", lambda *_args, **_kwargs:
        json.dumps({"results": [
            entry("valid", 1, "https://www.justice.gov/usao/pr/robbery"),
            entry("old", 72, "https://www.justice.gov/usao/pr/old"),
            entry("external", 1, "https://example.com/story"),
        ]}).encode())
    result = adapter.fetch()
    assert len(result.payloads) == 1
    observation = adapter.normalize(result.payloads[0])
    assert observation.title.startswith("U.S. Department of Justice:")
    assert observation.event_type == "crime_report"
    assert observation.fields["report_url"].startswith("https://www.justice.gov/")
    ingest_observation(conn, adapter, result.payloads[0],
                       result.feed_url, result.retrieved_at)
    event = repo.list_events(conn, event_type="crime_report")[0]
    assert event.source_ids and event.source_ids[0].startswith("doj_press:")
