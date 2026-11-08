"""No external requests: source downloads fail closed at the trust boundary."""
import io
import os
import urllib.request

import pytest

from newsroom.security import (
    _ApprovedRedirect, fetch_bytes, read_limited, redact_error,
    require_private_file,
)
from newsroom.storage.retention import disk_health, retention_audit


def test_feed_urls_require_https_and_approved_redirect_hosts():
    req = urllib.request.Request("https://data.gdeltproject.org/feed.zip")
    handler = _ApprovedRedirect({"data.gdeltproject.org"})
    with pytest.raises(ValueError, match="approved host"):
        handler.redirect_request(req, None, 302, "move", {},
                                 "http://127.0.0.1/internal")
    with pytest.raises(ValueError, match="approved host"):
        handler.redirect_request(req, None, 302, "move", {},
                                 "https://other.example/feed.zip")
    with pytest.raises(ValueError, match="approved host"):
        handler.redirect_request(req, None, 302, "move", {},
                                 "https://name:pass@data.gdeltproject.org/feed.zip")
    assert handler.redirect_request(
        req, None, 302, "move", {},
        "https://data.gdeltproject.org/new.zip").full_url.endswith("new.zip")


def test_fetch_limits_bytes_before_parsing_and_checks_final_url(monkeypatch):
    class Response(io.BytesIO):
        def __init__(self, body, url):
            super().__init__(body)
            self.url = url

        def geturl(self):
            return self.url

    class Opener:
        def __init__(self, url):
            self.url = url

        def open(self, req, timeout):
            return Response(b"abcde", self.url)

    monkeypatch.setattr(urllib.request, "build_opener",
                        lambda *args: Opener("https://example.gov/feed"))
    req = urllib.request.Request("https://example.gov/feed")
    assert fetch_bytes(req, timeout=2, max_bytes=5) == b"abcde"
    with pytest.raises(ValueError, match="size limit"):
        fetch_bytes(req, timeout=2, max_bytes=4)
    assert read_limited(io.BytesIO(b"abc"), 3) == b"abc"
    monkeypatch.setattr(urllib.request, "build_opener",
                        lambda *args: Opener("http://127.0.0.1/private"))
    with pytest.raises(ValueError, match="approved host"):
        fetch_bytes(req, timeout=2)
    with pytest.raises(ValueError, match="approved host"):
        fetch_bytes(urllib.request.Request("http://127.0.0.1/private"),
                    timeout=2)


def test_error_redaction_and_private_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", "test-secret-never-log")
    message = "https://api.eia.gov/x?api_key=test-secret-never-log&email=a@b.test"
    redacted = redact_error(ValueError(message))
    assert "test-secret-never-log" not in redacted
    assert "a@b.test" not in redacted
    path = tmp_path / ".env"
    path.write_text("API_KEY=not-printed")
    os.chmod(path, 0o644)
    with pytest.raises(PermissionError, match="mode 600"):
        require_private_file(path)
    os.chmod(path, 0o600)
    require_private_file(path)


def test_disk_guard_and_retention_are_read_only(tmp_path, settings, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("newsroom.storage.retention.shutil.disk_usage",
                        lambda path: SimpleNamespace(free=1 * 1024 ** 3))
    assert not disk_health(settings, root=tmp_path)["ok"]
    frames = tmp_path / "frames"
    frames.mkdir()
    old = frames / "old.png"
    old.write_bytes(b"not real image")
    os.utime(old, (1, 1))
    report = retention_audit(settings, root=tmp_path, now=1000000)
    assert report["frames"] == {"expired_files": 1, "expired_bytes": 14}
    assert report["action"] == "report_only"
    assert old.read_bytes() == b"not real image"


def test_low_disk_pauses_render_before_claim(conn, settings, monkeypatch):
    from newsroom.service import ServiceLoop

    monkeypatch.setattr("newsroom.storage.retention.disk_health",
                        lambda *args: {"ok": False})

    def no_claim(*args):
        raise AssertionError("render job should not have been claimed")

    monkeypatch.setattr("newsroom.storage.repositories.claim_next_job", no_claim)
    ServiceLoop(conn, settings).drain_jobs(max_jobs=1)
