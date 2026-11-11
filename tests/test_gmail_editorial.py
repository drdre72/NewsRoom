import base64
import copy
import json
import urllib.error
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qs, urlsplit

import pytest

from test_briefings import seeded_event
from newsroom.briefings import compose, record_rights, revise
from newsroom.editorial import inspect, request_changes
from newsroom.delivery import drain_review


class Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self.body


def prepared(conn, settings):
    seeded_event(conn)
    record_rights(conn, "usgs", "fixture-reviewer", "https://earthquake.usgs.gov/")
    return compose(conn, settings, "FREE")


def test_gmail_authorization_is_send_only_and_token_private(tmp_path):
    from newsroom.gmail_editorial import authorization_url, save_refresh_token

    query = parse_qs(urlsplit(authorization_url(
        "desktop-id", "http://127.0.0.1:8888/callback", "state", "verifier"
    )).query)
    assert query["scope"] == ["https://www.googleapis.com/auth/gmail.send"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["state"]
    env = tmp_path / ".env"
    env.write_text("UNRELATED=kept\nNEWSROOM_GMAIL_REFRESH_TOKEN=old\n")
    env.chmod(0o600)
    save_refresh_token("new", env)
    assert env.read_text() == "UNRELATED=kept\nNEWSROOM_GMAIL_REFRESH_TOKEN=new\n"
    assert env.stat().st_mode & 0o777 == 0o600


def test_gmail_mime_thread_and_receipt(conn, settings, monkeypatch):
    from newsroom.gmail_editorial import GmailMailer

    draft = prepared(conn, settings)
    monkeypatch.setenv("NEWSROOM_GMAIL_CLIENT_ID", "desktop-id")
    monkeypatch.setenv("NEWSROOM_GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("NEWSROOM_GMAIL_REFRESH_TOKEN", "test-refresh")
    monkeypatch.setattr(GmailMailer, "_access_token", lambda self: "test-access")
    seen = []

    def request(req, timeout, **kwargs):
        assert req.full_url == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
        assert req.get_header("Authorization") == "Bearer test-access"
        body = json.loads(req.data)
        mime = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(body["raw"])
        )
        seen.append((body, mime))
        assert mime["From"] == "editor@example.org"
        assert mime["To"] == "jarvintels@gmail.com"
        assert mime.get_body(preferencelist=("plain",)).get_content().startswith("NOT PUBLISHED")
        assert mime.get_body(preferencelist=("html",))
        return json.dumps({"id": f"google-{len(seen)}", "threadId": "thread-1"}).encode()

    monkeypatch.setattr("newsroom.gmail_editorial.fetch_bytes", request)
    mailer = GmailMailer("editor@example.org")
    assert drain_review(conn, mailer, draft_id=draft) == 1
    request_changes(conn, draft, "Verify title", "editor")
    revise(conn, settings, draft, "Verify title")
    assert drain_review(conn, mailer, draft_id=draft) == 1
    assert seen[0][1]["Subject"] == seen[1][1]["Subject"]
    assert seen[1][1]["In-Reply-To"] == seen[0][1]["Message-ID"]
    assert seen[1][0]["threadId"] == "thread-1"
    assert conn.execute("SELECT COUNT(*) FROM editorial_outbox WHERE status='sent'").fetchone()[0] == 2


def test_ambiguous_gmail_send_never_auto_retries(conn, settings, monkeypatch):
    from newsroom.gmail_editorial import GmailMailer

    draft = prepared(conn, settings)
    monkeypatch.setattr(GmailMailer, "_access_token", lambda self: "test-access")

    def uncertain(req, timeout, **kwargs):
        raise urllib.error.URLError("response lost")

    monkeypatch.setattr("newsroom.gmail_editorial.fetch_bytes", uncertain)
    assert drain_review(conn, GmailMailer("editor@example.org"), draft_id=draft) == 0
    assert conn.execute("SELECT status FROM editorial_outbox").fetchone()[0] == "uncertain"
    assert drain_review(conn, GmailMailer("editor@example.org"), draft_id=draft) == 0


def test_real_send_requires_flag_and_exact_draft(conn, settings):
    from newsroom.gmail_editorial import real_send_review

    draft = prepared(conn, settings)
    revision = inspect(conn, draft)["revision"]
    with pytest.raises(ValueError, match="disabled"):
        real_send_review(conn, settings, draft, 1, revision["content_sha256"],
                         "jarvintels@gmail.com")
    assert conn.execute("SELECT status FROM editorial_outbox").fetchone()[0] == "pending"


def test_explicit_real_send_path_is_scoped_to_one_revision(conn, settings, monkeypatch):
    from newsroom.config import Settings
    from newsroom.delivery import FakeMailer
    from newsroom.gmail_editorial import real_send_review

    draft = prepared(conn, settings)
    revision = inspect(conn, draft)["revision"]
    values = copy.deepcopy(settings.raw())
    values["editorial"]["real_mail_enabled"] = True
    values["editorial"]["sender"] = "editor@example.org"
    enabled = Settings(values, settings.root)
    monkeypatch.setenv("NEWSROOM_GMAIL_SENDER", "editor@example.org")
    mailer = FakeMailer()
    monkeypatch.setattr("newsroom.gmail_editorial.GmailMailer", lambda sender: mailer)
    with pytest.raises(ValueError, match="confirmation"):
        real_send_review(conn, enabled, draft, 1, revision["content_sha256"], "wrong@example.org")
    assert real_send_review(conn, enabled, draft, 1, revision["content_sha256"],
                            "jarvintels@gmail.com") == 1
    assert len(mailer.messages) == 1
    with pytest.raises(ValueError, match="pending"):
        real_send_review(conn, enabled, draft, 1, revision["content_sha256"],
                         "jarvintels@gmail.com")


def test_gmail_cli_defaults_to_read_only(conn, settings, monkeypatch, capsys):
    from newsroom.cli import main

    draft = prepared(conn, settings)
    digest = inspect(conn, draft)["revision"]["content_sha256"]
    assert main(["newsletter", "gmail-auth"]) == 1
    assert main(["newsletter", "gmail-send-test", draft, "--revision", "1",
                 "--hash", digest]) == 0
    assert json.loads(capsys.readouterr().out)["messages_sent"] == 0
    assert conn.execute("SELECT status FROM editorial_outbox").fetchone()[0] == "pending"


def test_import_gmail_client_keeps_other_credentials_private(tmp_path):
    from newsroom.gmail_editorial import import_client_credentials

    client = tmp_path / "desktop.json"
    client.write_text(json.dumps({"installed": {
        "client_id": "sample.apps.googleusercontent.com",
        "client_secret": "sample-secret",
        "redirect_uris": ["http://localhost"],
    }}))
    client.chmod(0o600)
    env = tmp_path / ".env"
    env.write_text("NEWSROOM_YT_REFRESH_TOKEN=existing\n")
    env.chmod(0o600)
    import_client_credentials(client, env)
    text = env.read_text()
    assert "NEWSROOM_YT_REFRESH_TOKEN=existing" in text
    assert "NEWSROOM_GMAIL_CLIENT_ID=sample.apps.googleusercontent.com" in text
    assert "NEWSROOM_GMAIL_CLIENT_SECRET=sample-secret" in text
    assert env.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="already"):
        client.write_text(json.dumps({"installed": {
            "client_id": "different.apps.googleusercontent.com",
            "client_secret": "other-secret",
        }}))
        import_client_credentials(client, env)
    assert env.read_text() == text
    client.chmod(0o644)
    with pytest.raises(PermissionError):
        import_client_credentials(client, env)


def test_import_rejects_non_desktop_credentials(tmp_path):
    from newsroom.gmail_editorial import import_client_credentials

    client = tmp_path / "web.json"
    client.write_text(json.dumps({"web": {
        "client_id": "sample.apps.googleusercontent.com", "client_secret": "secret",
    }}))
    client.chmod(0o600)
    with pytest.raises(ValueError, match="desktop"):
        import_client_credentials(client, tmp_path / ".env")
    assert not (tmp_path / ".env").exists()
