"""TikTok drafts API — OAuth shape, inbox upload, crash-safe resume."""
import hashlib
import json
import urllib.error
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

import pytest

import newsroom.publish.tiktok_api as api
from newsroom.publish.tiktok_api import (
    _upload_url_ok, authorization_url, exchange_auth_code, fetch_status,
    inbox_init, push_to_inbox, refresh_access_token, save_credentials,
)


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status = status
        self.headers = headers or {}
        self.body = json.dumps(body or {}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def _creds(monkeypatch):
    monkeypatch.setenv("NEWSROOM_TIKTOK_CLIENT_KEY", "ck-test")
    monkeypatch.setenv("NEWSROOM_TIKTOK_CLIENT_SECRET", "cs-test")


class TestAuth:
    def test_authorize_url_shape_and_pkce(self):
        url = authorization_url("ck-1", "http://127.0.0.1:8789/callback",
                                "st", "ver")
        q = parse_qs(urlsplit(url).query)
        assert url.startswith("https://www.tiktok.com/v2/auth/authorize/")
        assert q["client_key"] == ["ck-1"]
        assert q["scope"] == ["video.upload"]          # drafts only
        assert q["response_type"] == ["code"]
        assert q["redirect_uri"] == ["http://127.0.0.1:8789/callback"]
        assert q["code_challenge_method"] == ["S256"]
        assert q["code_challenge"][0] != "ver"

    def test_exchange_posts_form_and_returns_tokens(self, monkeypatch):
        _creds(monkeypatch)
        seen = {}

        def fake_open(req, timeout):
            seen["url"] = req.full_url
            seen["body"] = urllib.parse.parse_qs(req.data.decode())
            return FakeResponse(200, {
                "access_token": "act.x", "refresh_token": "rft.y",
                "open_id": "oid-1", "token_type": "Bearer"})

        monkeypatch.setattr(api.urllib.request, "urlopen", fake_open)
        out = exchange_auth_code("code123", "http://127.0.0.1:8789/cb", "v")
        assert seen["url"] == ("https://open.tiktokapis.com"
                               "/v2/oauth/token/")
        assert seen["body"]["grant_type"] == ["authorization_code"]
        assert seen["body"]["client_key"] == ["ck-test"]
        assert seen["body"]["code_verifier"] == ["v"]
        assert out == {"refresh_token": "rft.y", "open_id": "oid-1"}

    def test_refresh_returns_access_and_rotated_refresh(self, monkeypatch):
        _creds(monkeypatch)
        monkeypatch.setenv("NEWSROOM_TIKTOK_REFRESH_TOKEN", "rft.old")

        def fake_open(req, timeout):
            body = urllib.parse.parse_qs(req.data.decode())
            assert body["grant_type"] == ["refresh_token"]
            assert body["refresh_token"] == ["rft.old"]
            return FakeResponse(200, {
                "access_token": "act.new", "refresh_token": "rft.new",
                "expires_in": 86400})

        monkeypatch.setattr(api.urllib.request, "urlopen", fake_open)
        access, rotated = refresh_access_token()
        assert access == "act.new" and rotated == "rft.new"

    def test_refresh_without_token_errors(self, monkeypatch):
        monkeypatch.delenv("NEWSROOM_TIKTOK_REFRESH_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="tiktok auth"):
            refresh_access_token()

    def test_save_credentials_private_and_replaces(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("KEEP=1\nNEWSROOM_TIKTOK_REFRESH_TOKEN=old\n")
        env.chmod(0o600)
        save_credentials({"refresh_token": "new", "open_id": "o1"}, env)
        text = env.read_text()
        assert env.stat().st_mode & 0o777 == 0o600
        assert "KEEP=1" in text
        assert "NEWSROOM_TIKTOK_REFRESH_TOKEN=new" in text
        assert "NEWSROOM_TIKTOK_OPEN_ID=o1" in text
        assert "old" not in text


class TestInboxApi:
    def test_init_single_chunk_small_video(self, monkeypatch):
        seen = {}

        def fake_open(req, timeout):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode())
            seen["auth"] = req.get_header("Authorization")
            return FakeResponse(200, {
                "data": {"publish_id": "v_inbox_1",
                         "upload_url": "https://open-upload.tiktokapis.com"
                                       "/video/?upload_id=u1"}})

        monkeypatch.setattr(api.urllib.request, "urlopen", fake_open)
        out = inbox_init(30_000_000, "tok")
        assert seen["url"].endswith("/v2/post/publish/inbox/video/init/")
        assert seen["auth"] == "Bearer tok"
        si = seen["body"]["source_info"]
        assert si["source"] == "FILE_UPLOAD"
        assert si["video_size"] == 30_000_000
        assert si["chunk_size"] == api._CHUNK
        assert si["total_chunk_count"] == 1
        assert out["publish_id"] == "v_inbox_1"

    def test_init_rejects_bad_upload_url(self, monkeypatch):
        monkeypatch.setattr(api.urllib.request, "urlopen", lambda r, timeout:
                            FakeResponse(200, {"data": {
                                "publish_id": "p",
                                "upload_url": "https://evil.example/up"}}))
        with pytest.raises(RuntimeError, match="invalid"):
            inbox_init(10, "tok")

    def test_upload_url_allowlist(self):
        assert _upload_url_ok(
            "https://open-upload.tiktokapis.com/video/?u=1")
        assert _upload_url_ok("https://upload.us.tiktokapis.com/v/?u=1")
        assert not _upload_url_ok("https://tiktokapis.com.evil.com/v")
        assert not _upload_url_ok("https://nottiktokapis.com/v")
        assert not _upload_url_ok("http://open-upload.tiktokapis.com/v")
        assert not _upload_url_ok(
            "https://user:pw@open-upload.tiktokapis.com/v")

    def test_fetch_status(self, monkeypatch):
        seen = {}

        def fake_open(req, timeout):
            seen["body"] = json.loads(req.data.decode())
            return FakeResponse(200, {"data": {
                "status": "SEND_TO_USER_INBOX", "fail_reason": ""}})

        monkeypatch.setattr(api.urllib.request, "urlopen", fake_open)
        st = fetch_status("pid-1", "tok")
        assert seen["body"] == {"publish_id": "pid-1"}
        assert st["status"] == "SEND_TO_USER_INBOX"


class TestPushToInbox:
    def _manifest(self, tmp_path, name="vid.mp4", content=b"video-bytes"):
        video = tmp_path / name
        video.write_bytes(content)
        return {"video": {
            "path": str(video),
            "hash": hashlib.sha256(content).hexdigest(),
            "width": 1080, "height": 1920, "duration_s": 75}}

    def test_happy_path_init_put_status(self, tmp_path, monkeypatch):
        manifest = self._manifest(tmp_path)
        calls = []

        def fake_open(req, timeout):
            calls.append(req)
            if "inbox/video/init" in req.full_url:
                return FakeResponse(200, {"data": {
                    "publish_id": "pid-9",
                    "upload_url": "https://open-upload.tiktokapis.com/v/?u=1"}})
            if req.get_method() == "PUT":
                assert req.get_header("Content-range") == (
                    "bytes 0-10/11")
                assert req.data == b"video-bytes"
                return FakeResponse(201)
            return FakeResponse(200, {"data": {
                "status": "SEND_TO_USER_INBOX"}})

        monkeypatch.setattr(api.urllib.request, "urlopen", fake_open)
        out = push_to_inbox(manifest, "tok", asset_id="a1",
                            session_dir=tmp_path / "sess")
        assert out["ok"] and out["publish_id"] == "pid-9"
        methods = [c.get_method() for c in calls]
        assert methods == ["POST", "PUT", "POST"]
        assert "status/fetch" in calls[-1].full_url
        journal = tmp_path / "sess" / "a1.json"
        assert journal.exists() and journal.stat().st_mode & 0o777 == 0o600

    def test_resume_delivered_without_reinit(self, tmp_path, monkeypatch):
        manifest = self._manifest(tmp_path)
        calls = {"init": 0, "put": 0, "status": 0}

        def fake_open(req, timeout):
            if "inbox/video/init" in req.full_url:
                calls["init"] += 1
                return FakeResponse(200, {"data": {
                    "publish_id": "pid-9",
                    "upload_url": "https://open-upload.tiktokapis.com/v/?u=1"}})
            if req.get_method() == "PUT":
                calls["put"] += 1
                raise urllib.error.URLError("lost after bytes sent")
            calls["status"] += 1
            return FakeResponse(200, {"data": {
                "status": "SEND_TO_USER_INBOX"}})

        monkeypatch.setattr(api.urllib.request, "urlopen", fake_open)
        first = push_to_inbox(manifest, "tok", asset_id="a2",
                              session_dir=tmp_path / "sess")
        assert first["ok"] is False          # PUT blew up
        second = push_to_inbox(manifest, "tok", asset_id="a2",
                               session_dir=tmp_path / "sess")
        assert second["ok"] and second["resumed"]
        # No second init, no second PUT — status reconciled it.
        assert calls == {"init": 1, "put": 1, "status": 1}

    def test_api_error_fails_closed(self, tmp_path, monkeypatch):
        manifest = self._manifest(tmp_path)
        monkeypatch.setattr(api.urllib.request, "urlopen", lambda r, timeout:
                            FakeResponse(200, {"error": {
                                "code": "access_token_invalid",
                                "message": "bad token"}}))
        out = push_to_inbox(manifest, "tok", asset_id="a3",
                            session_dir=tmp_path / "sess")
        assert out["ok"] is False

    def test_video_mismatch_refuses(self, tmp_path, monkeypatch):
        manifest = self._manifest(tmp_path)
        # clobber the file after the manifest hash was taken
        (tmp_path / "vid.mp4").write_bytes(b"different")
        monkeypatch.setattr(api.urllib.request, "urlopen",
                            lambda r, timeout: pytest.fail("no network expected"))
        out = push_to_inbox(manifest, "tok", asset_id="a4",
                            session_dir=tmp_path / "sess")
        assert out["ok"] is False and "changed" in out["error"]
