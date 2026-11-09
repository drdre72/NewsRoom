"""TikTok Content Posting API — OAuth v2 + upload-to-inbox drafts.

Scope: `video.upload` only. Media lands in the creator's TikTok inbox as
a *draft* and a human publishes in-app — TikTok's Direct Post audit
rejects self-upload tools, so inbox drafts are the compliant ceiling.

OAuth: desktop loopback authorization code flow with PKCE (the redirect
URI is fixed at registration: http://127.0.0.1:8789/callback). Access
tokens live 24h; refresh tokens 365d and ROTATE on every refresh — the
new refresh token must be persisted each time.

Upload: POST /v2/post/publish/inbox/video/init/ -> upload_url -> PUT
chunks -> status/fetch. The session journal is written before the first
byte; retries reconcile by publish_id status. The worst case of a lost
upload is a duplicate inbox draft, never an unwanted public post.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from ..logging import get_logger, log_event
from ..security import redact_error
from .upload_session import load_session, locked_session, save_session

log = get_logger("tiktok_api")

_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
_API_BASE = "https://open.tiktokapis.com/v2"
_SCOPE = "video.upload"
_PORT = 8789                   # must equal the registered redirect URI's port
_CHUNK = 64 * 1024 * 1024      # TikTok: 5–64 MB per non-final chunk
_MAX_SINGLE = _CHUNK           # one PUT for videos ≤64 MB


# ------------------------------------------------------------------ OAuth

def _client_creds() -> tuple[str, str]:
    key = os.environ.get("NEWSROOM_TIKTOK_CLIENT_KEY", "")
    secret = os.environ.get("NEWSROOM_TIKTOK_CLIENT_SECRET", "")
    if not key or not secret:
        raise RuntimeError(
            "TikTok client credentials not set — put "
            "NEWSROOM_TIKTOK_CLIENT_KEY and NEWSROOM_TIKTOK_CLIENT_SECRET "
            "in .env (developers.tiktok.com → your app)")
    return key, secret


def authorization_url(client_key: str, redirect_uri: str, state: str,
                      verifier: str) -> str:
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return _AUTH_URL + "?" + urllib.parse.urlencode({
        "client_key": client_key, "redirect_uri": redirect_uri,
        "response_type": "code", "scope": _SCOPE, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })


def _token_request(params: dict) -> dict:
    key, secret = _client_creds()
    data = urllib.parse.urlencode(
        {"client_key": key, "client_secret": secret, **params}).encode()
    req = urllib.request.Request(
        _TOKEN_URL, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"TikTok token endpoint rejected ({exc.code}): "
            f"{redact_error(exc)}") from None
    if body.get("error"):
        raise RuntimeError(f"TikTok token error: {body['error']}")
    return body


def exchange_auth_code(code: str, redirect_uri: str, verifier: str) -> dict:
    body = _token_request({
        "code": code, "grant_type": "authorization_code",
        "redirect_uri": redirect_uri, "code_verifier": verifier})
    if not body.get("refresh_token"):
        raise RuntimeError("TikTok returned no refresh token; retry auth")
    return {"refresh_token": body["refresh_token"],
            "open_id": body.get("open_id", "")}


def authorize_desktop(timeout: int = 300, announce=print) -> dict:
    """Fixed-port loopback auth — TikTok requires an exact-match
    redirect URI, so the port is pinned to the registered value."""
    key, _ = _client_creds()
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    result: dict[str, str] = {}
    redirect_uri = f"http://127.0.0.1:{_PORT}/callback"
    auth_url = authorization_url(key, redirect_uri, state, verifier)

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
            body = b"Authorization received. You can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass  # never log the authorization code or query string

    with HTTPServer(("127.0.0.1", _PORT), Callback) as server:
        announce("Open this URL on this Mac to authorize TikTok:")
        announce(f"http://127.0.0.1:{_PORT}/start")
        deadline = time.monotonic() + timeout
        while "code" not in result and "error" not in result:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("TikTok authorization timed out")
            server.timeout = min(remaining, 1)
            server.handle_request()
    if result.get("error"):
        raise RuntimeError(f"TikTok authorization denied: {result['error']}")
    if not result.get("code"):
        raise RuntimeError("TikTok authorization returned no code")
    return exchange_auth_code(result["code"], redirect_uri, verifier)


def save_credentials(tokens: dict, env_file: Path) -> None:
    """Replace our TikTok keys in .env; never print or commit them."""
    from ..security import require_private_file
    require_private_file(env_file)
    drop = {"NEWSROOM_TIKTOK_REFRESH_TOKEN", "NEWSROOM_TIKTOK_OPEN_ID"}
    lines = [l for l in env_file.read_text().splitlines()
             if l.strip().split("=", 1)[0].strip() not in drop]
    lines.append(f"NEWSROOM_TIKTOK_REFRESH_TOKEN={tokens['refresh_token']}")
    if tokens.get("open_id"):
        lines.append(f"NEWSROOM_TIKTOK_OPEN_ID={tokens['open_id']}")
    tmp = env_file.with_name(env_file.name + ".oauth.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, env_file)
    finally:
        tmp.unlink(missing_ok=True)


def refresh_access_token() -> tuple[str, str | None]:
    """Mint a 24h access token. TikTok rotates the refresh token on each
    call — returns (access, new_refresh_or_None); persist the new one."""
    refresh = os.environ.get("NEWSROOM_TIKTOK_REFRESH_TOKEN", "")
    if not refresh:
        raise RuntimeError(
            "NEWSROOM_TIKTOK_REFRESH_TOKEN not set — run "
            "`python main.py tiktok auth` first")
    body = _token_request({"grant_type": "refresh_token",
                           "refresh_token": refresh})
    access = body.get("access_token")
    if not access:
        raise RuntimeError("TikTok refresh returned no access token")
    return access, body.get("refresh_token")


# ------------------------------------------------------------------ API

def _api_post(path: str, payload: dict, token: str) -> dict:
    req = urllib.request.Request(
        _API_BASE + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"TikTok API rejected ({exc.code}): {redact_error(exc)}"
        ) from None
    err = body.get("error") or {}
    if err.get("code") not in (None, "", "ok"):
        raise RuntimeError(f"TikTok API error: {err.get('code')}")
    return body.get("data") or {}


def _upload_url_ok(url: str) -> bool:
    p = urllib.parse.urlsplit(url)
    return (p.scheme == "https"
            and (p.hostname or "").endswith(".tiktokapis.com")
            and p.port in (None, 443) and not p.username
            and not p.password and not p.fragment)


def inbox_init(size: int, token: str) -> dict:
    """One upload session: {publish_id, upload_url}. Chunk count follows
    TikTok's floor(video_size/chunk_size) rule — the final chunk absorbs
    the remainder and may reach 128 MB."""
    chunk_size = _CHUNK
    count = max(1, size // chunk_size)
    data = _api_post("/post/publish/inbox/video/init/", {
        "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                        "chunk_size": chunk_size,
                        "total_chunk_count": count}}, token)
    pid, url = data.get("publish_id", ""), data.get("upload_url", "")
    if not pid or not _upload_url_ok(url):
        raise RuntimeError("invalid TikTok upload session response")
    return {"publish_id": pid, "upload_url": url}


def fetch_status(publish_id: str, token: str) -> dict:
    """data.status — SEND_TO_USER_INBOX means the draft is delivered."""
    data = _api_post("/post/publish/status/fetch/",
                     {"publish_id": publish_id}, token)
    return {"status": data.get("status", ""),
            "fail_reason": data.get("fail_reason", "")}


def _put_chunk(upload_url: str, body: bytes, first: int, total: int) -> None:
    last = first + len(body) - 1
    req = urllib.request.Request(
        upload_url, data=body, method="PUT",
        headers={"Content-Type": "video/mp4",
                 "Content-Length": str(len(body)),
                 "Content-Range": f"bytes {first}-{last}/{total}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError(
                    f"TikTok chunk rejected ({resp.status})")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"TikTok chunk upload failed ({exc.code}): "
            f"{redact_error(exc)}") from None


def _put_all(video_path: Path, upload_url: str, size: int) -> None:
    count = max(1, size // _CHUNK)
    with video_path.open("rb") as fh:
        first = 0
        for i in range(count):
            body = fh.read() if i == count - 1 else fh.read(_CHUNK)
            if not body:
                break
            _put_chunk(upload_url, body, first, size)
            first += len(body)


def push_to_inbox(manifest: dict, token: str, *,
                  asset_id: str | None = None,
                  session_dir: Path | None = None) -> dict:
    """Send a rendered video to the creator's TikTok inbox as a draft.

    Session is journaled before the first byte; retries reconcile via
    publish_id status rather than blindly re-sending.
    """
    if not asset_id:
        raise ValueError("asset_id required for crash-safe push")
    video_path = Path(manifest["video"]["path"])
    if not video_path.exists():
        return {"ok": False, "error": f"video file missing: {video_path}"}
    digest = manifest["video"].get("hash")
    size = video_path.stat().st_size
    if not digest or size == 0:
        return {"ok": False, "error": "missing video hash or empty file"}
    try:
        with video_path.open("rb") as fh:
            if hashlib.file_digest(fh, "sha256").hexdigest() != digest:
                return {"ok": False, "error": "video changed since render"}
        with locked_session(asset_id, session_dir) as journal:
            state = load_session(journal) or {}
            if (state.get("hash") == digest and state.get("publish_id")
                    and state.get("size") == size):
                st = fetch_status(state["publish_id"], token)
                if st["status"] == "SEND_TO_USER_INBOX":
                    return {"ok": True, "publish_id": state["publish_id"],
                            "resumed": True}
                # Unknown or failed — a duplicate draft is the worst case;
                # never a public post. Fall through to a fresh session.
            init = inbox_init(size, token)
            state = {"publish_id": init["publish_id"],
                     "upload_url": init["upload_url"],
                     "hash": digest, "size": size}
            save_session(journal, state)        # before the first byte
            _put_all(video_path, init["upload_url"], size)
            st = fetch_status(init["publish_id"], token)
            state["status"] = st["status"]
            save_session(journal, state)
            return {"ok": True, "publish_id": init["publish_id"],
                    "status": st["status"]}
    except (OSError, RuntimeError, KeyError, ValueError):
        log_event(log, 30, "tiktok inbox push paused; session retained",
                  asset_id=asset_id)
        return {"ok": False, "error": "push paused; retry to reconcile"}
