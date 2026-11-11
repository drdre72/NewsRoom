from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .briefings import _hash
from .config import Settings
from .delivery import AmbiguousDelivery, drain_review
from .editorial import inspect
from .security import fetch_bytes, require_private_file

_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN = "https://oauth2.googleapis.com/token"
_SEND = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def _client_creds() -> tuple[str, str]:
    cid = os.environ.get("NEWSROOM_GMAIL_CLIENT_ID", "")
    secret = os.environ.get("NEWSROOM_GMAIL_CLIENT_SECRET", "")
    if not cid or not secret:
        raise RuntimeError("NEWSROOM_GMAIL_CLIENT_ID and NEWSROOM_GMAIL_CLIENT_SECRET required")
    return cid, secret


def authorization_url(client_id: str, redirect_uri: str, state: str, verifier: str) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(
        verifier.encode("ascii")
    ).digest()).rstrip(b"=").decode("ascii")
    return _AUTH + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri,
        "response_type": "code", "scope": _SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })


def exchange_auth_code(code: str, redirect_uri: str, verifier: str) -> str:
    cid, secret = _client_creds()
    req = urllib.request.Request(_TOKEN, data=urllib.parse.urlencode({
        "client_id": cid, "client_secret": secret, "code": code,
        "code_verifier": verifier, "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode(), method="POST")
    body = json.loads(fetch_bytes(req, timeout=15, max_bytes=65536))
    if not body.get("refresh_token"):
        raise RuntimeError("Gmail authorization did not return a refresh token")
    if _SCOPE not in body.get("scope", _SCOPE).split():
        raise RuntimeError("Gmail send-only scope not granted")
    return body["refresh_token"]


def authorize_desktop(timeout: int = 300, announce=print) -> str:
    cid, _ = _client_creds()
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    result: dict[str, str] = {}
    auth_url = ""

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/start":
                self.send_response(302)
                self.send_header("Location", auth_url)
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/callback" or params.get("state") != [state]:
                self.send_error(400, "Invalid OAuth callback")
                return
            result["code"] = params.get("code", [""])[0]
            result["error"] = params.get("error", [""])[0]
            body = b"Gmail send-only authorization received. You can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        uri = f"http://127.0.0.1:{server.server_port}/callback"
        auth_url = authorization_url(cid, uri, state, verifier)
        announce("Open this local URL on this Mac to authorize Gmail send-only access:")
        announce(f"http://127.0.0.1:{server.server_port}/start")
        deadline = time.monotonic() + timeout
        while "code" not in result and "error" not in result:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Gmail authorization timed out")
            server.timeout = min(remaining, 1)
            server.handle_request()
    if result.get("error") or not result.get("code"):
        raise RuntimeError("Gmail authorization was denied")
    return exchange_auth_code(result["code"], uri, verifier)


def _store_keys(env_file: Path, values: dict[str, str]) -> None:
    if any(not value or "\r" in value or "\n" in value for value in values.values()):
        raise ValueError("invalid Gmail credentials")
    if env_file.exists():
        require_private_file(env_file)
        lines = env_file.read_text().splitlines()
    else:
        lines = []
    lines = [line for line in lines
             if line.strip().split("=", 1)[0].strip() not in values]
    lines.extend(f"{key}={value}" for key, value in values.items())
    tmp = env_file.with_name(env_file.name + ".gmail-oauth.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, env_file)
    finally:
        tmp.unlink(missing_ok=True)


def save_refresh_token(token: str, env_file: Path) -> None:
    _store_keys(env_file, {"NEWSROOM_GMAIL_REFRESH_TOKEN": token})


def import_client_credentials(client_json: Path, env_file: Path) -> None:
    require_private_file(client_json)
    if client_json.stat().st_size > 65536:
        raise ValueError("Gmail OAuth client JSON exceeds size limit")
    data = json.loads(client_json.read_text(encoding="utf-8"))
    client = data.get("installed", {})
    if not isinstance(client, dict):
        raise ValueError("desktop OAuth client JSON required")
    cid = client.get("client_id")
    secret = client.get("client_secret")
    if not isinstance(cid, str) or not cid.endswith(".apps.googleusercontent.com"):
        raise ValueError("desktop OAuth client JSON required")
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("desktop OAuth client JSON required")
    if env_file.exists():
        require_private_file(env_file)
        existing = dict(line.split("=", 1) for line in env_file.read_text().splitlines()
                        if "=" in line and line.split("=", 1)[0] in (
                            "NEWSROOM_GMAIL_CLIENT_ID", "NEWSROOM_GMAIL_CLIENT_SECRET"
                        ))
        if existing and existing != {
            "NEWSROOM_GMAIL_CLIENT_ID": cid, "NEWSROOM_GMAIL_CLIENT_SECRET": secret
        }:
            raise ValueError("Gmail client already configured; refusing to replace it")
    _store_keys(env_file, {
        "NEWSROOM_GMAIL_CLIENT_ID": cid, "NEWSROOM_GMAIL_CLIENT_SECRET": secret,
    })


class GmailMailer:
    def __init__(self, sender: str):
        name, addr = parseaddr(sender)
        if name or sender != addr or "@" not in addr or any(c in addr for c in "\r\n"):
            raise ValueError("authorized sender must be a plain email address")
        self.sender = addr

    def _access_token(self) -> str:
        refresh = os.environ.get("NEWSROOM_GMAIL_REFRESH_TOKEN", "")
        if not refresh:
            raise RuntimeError("NEWSROOM_GMAIL_REFRESH_TOKEN required; authorize via local Gmail OAuth")
        cid, secret = _client_creds()
        req = urllib.request.Request(_TOKEN, data=urllib.parse.urlencode({
            "client_id": cid, "client_secret": secret, "refresh_token": refresh,
            "grant_type": "refresh_token",
        }).encode(), method="POST")
        body = json.loads(fetch_bytes(req, timeout=15, max_bytes=65536))
        if not body.get("access_token"):
            raise RuntimeError("Gmail access-token refresh failed")
        return body["access_token"]

    def _message_id(self, message_id: str) -> str:
        local = message_id.lstrip("<").split("@", 1)[0]
        if not local.startswith("review_") or not local.replace("_", "").isalnum():
            raise ValueError("invalid review message id")
        return f"<{local}@{self.sender.split('@', 1)[1]}>"

    def send_review(self, message: dict) -> str:
        if parseaddr(message["to"])[1] != message["to"]:
            raise ValueError("invalid editorial recipient")
        mime = EmailMessage()
        mime["From"] = self.sender
        mime["To"] = message["to"]
        mime["Subject"] = message["subject"]
        mime["Date"] = formatdate(localtime=False)
        rfc_id = self._message_id(message["message_id"])
        mime["Message-ID"] = rfc_id
        if message.get("in_reply_to"):
            parent = self._message_id(message["in_reply_to"])
            mime["In-Reply-To"] = parent
            mime["References"] = parent
        mime.set_content(message["body"])
        mime.add_alternative(message["html"], subtype="html")
        payload = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")}
        if message.get("thread_id"):
            payload["threadId"] = message["thread_id"]
        req = urllib.request.Request(
            _SEND, data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Bearer {self._access_token()}",
                     "Content-Type": "application/json"},
        )
        try:
            response = json.loads(fetch_bytes(req, timeout=30, max_bytes=65536))
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise AmbiguousDelivery("Gmail response may have been accepted") from None
            raise RuntimeError(f"Gmail send rejected (HTTP {exc.code})") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise AmbiguousDelivery("Gmail delivery response unavailable") from None
        if not response.get("id"):
            raise AmbiguousDelivery("Gmail accepted request without a usable receipt")
        return json.dumps({"id": response["id"], "threadId": response.get("threadId"),
                           "rfcMessageId": rfc_id})


def real_send_review(conn, settings: Settings, draft_id: str, revision_id: int,
                     content_sha256: str, confirmed_recipient: str) -> int:
    if not settings.get("editorial.real_mail_enabled", False):
        raise ValueError("real editorial mail is disabled in config")
    recipient = settings.get("editorial.to", "")
    if not recipient or confirmed_recipient != recipient:
        raise ValueError("editorial recipient confirmation mismatch")
    sender = settings.get("editorial.sender", "")
    if not sender:
        raise ValueError("editorial.sender must be configured")
    if os.environ.get("NEWSROOM_GMAIL_SENDER", sender) != sender:
        raise ValueError("NEWSROOM_GMAIL_SENDER must match editorial.sender")
    snapshot = inspect(conn, draft_id)
    rev = snapshot["revision"]
    if snapshot["draft"]["status"] != "READY_FOR_REVIEW" or rev["revision_id"] != revision_id:
        raise ValueError("draft is not ready at requested revision")
    if rev["content_sha256"] != content_sha256 or _hash(
        rev["text"], rev["html"], rev["evidence"], rev["event_refs"]
    ) != content_sha256:
        raise ValueError("reviewed content hash mismatch")
    job = conn.execute(
        "SELECT status FROM editorial_outbox WHERE draft_id=? AND revision_id=? "
        "AND recipient=? AND kind='review'",
        (draft_id, revision_id, recipient),
    ).fetchone()
    if not job or job["status"] != "pending":
        raise ValueError("no pending review email for exact revision and recipient")
    return drain_review(conn, GmailMailer(sender), batch_size=1,
                        draft_id=draft_id, revision_id=revision_id)
