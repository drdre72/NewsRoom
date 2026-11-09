"""YouTube Data API v3 — desktop OAuth, resumable upload, playlists.

No Google client library: raw HTTP via urllib, same as every adapter.
The LLM never decides what uploads — the publication gate is
deterministic, and `publishing.automatic` is the master switch (§66).

OAuth: desktop loopback authorization code flow with PKCE. The refresh
token is stored privately in .env, never printed to stdout.

Upload: resumable videos.insert — initiate session, chunk the MP4,
finalize. Title/description/chapters come from the render manifest.
Shorts (vertical 1080x1920) get `#Shorts` in the description.

Quota (June 2026): videos.insert has its own bucket — 100 uploads/day
at 1 unit each, independent of the 10,000-unit shared pool.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

from ..config import Settings
from ..logging import get_logger, log_event
from ..security import redact_error
from .upload_session import locked_session, load_session, save_session, session_url_ok

log = get_logger("youtube")

# Google OAuth endpoints (public, documented).
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
_API_BASE = "https://www.googleapis.com/youtube/v3"
_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
_PLAYLIST_SCOPE = "https://www.googleapis.com/auth/youtube"
_CHUNK = 8 * 1024 * 1024          # 8 MiB resumable chunks


# ------------------------------------------------------------------ OAuth

def _client_creds() -> tuple[str, str]:
    """Read client_id + client_secret from .env."""
    cid = os.environ.get("NEWSROOM_YT_CLIENT_ID", "")
    secret = os.environ.get("NEWSROOM_YT_CLIENT_SECRET", "")
    if not cid or not secret:
        raise RuntimeError(
            "YouTube client credentials not set — put "
            "NEWSROOM_YT_CLIENT_ID and NEWSROOM_YT_CLIENT_SECRET in .env "
            "(from Google Cloud Console → APIs → YouTube Data API v3 → "
            "OAuth client ID → Desktop app)")
    return cid, secret


def authorization_url(client_id: str, redirect_uri: str, state: str,
                      verifier: str) -> str:
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return _AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri,
        "response_type": "code", "scope": _SCOPE + " " + _PLAYLIST_SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })


def exchange_auth_code(code: str, redirect_uri: str, verifier: str) -> str:
    cid, secret = _client_creds()
    data = urllib.parse.urlencode({
        "client_id": cid, "client_secret": secret, "code": code,
        "code_verifier": verifier, "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(_TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        refresh = json.loads(resp.read()).get("refresh_token")
    if not refresh:
        raise RuntimeError("Google did not return a refresh token; retry authorization")
    return refresh


def authorize_desktop(timeout: int = 300, announce=print) -> str:
    """Listen only on loopback for one matching OAuth callback."""
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
            body = b"Authorization received. You can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass  # Never log the authorization code or query string.

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        uri = f"http://127.0.0.1:{server.server_port}/callback"
        auth_url = authorization_url(cid, uri, state, verifier)
        announce("Open this URL on this Mac to authorize YouTube:")
        announce(f"http://127.0.0.1:{server.server_port}/start")
        deadline = time.monotonic() + timeout
        while "code" not in result and "error" not in result:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("YouTube authorization timed out")
            server.timeout = min(remaining, 1)
            server.handle_request()
    if result.get("error"):
        raise RuntimeError("YouTube authorization was denied")
    if not result.get("code"):
        raise RuntimeError("YouTube authorization returned no code")
    return exchange_auth_code(result["code"], uri, verifier)


def save_refresh_token(token: str, env_file: Path) -> None:
    """Replace only our token key in .env; never print or store it in git."""
    from ..security import require_private_file
    require_private_file(env_file)
    lines = env_file.read_text().splitlines()
    lines = [line for line in lines
             if not line.strip().startswith("NEWSROOM_YT_REFRESH_TOKEN=")]
    lines.append(f"NEWSROOM_YT_REFRESH_TOKEN={token}")
    tmp = env_file.with_name(env_file.name + ".oauth.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, env_file)
    finally:
        tmp.unlink(missing_ok=True)


def refresh_access_token() -> str:
    """Mint a 1-hour access token from the stored refresh token."""
    refresh = os.environ.get("NEWSROOM_YT_REFRESH_TOKEN", "")
    if not refresh:
        raise RuntimeError(
            "NEWSROOM_YT_REFRESH_TOKEN not set in .env — run "
            "`python main.py youtube auth` first")
    cid, secret = _client_creds()
    data = urllib.parse.urlencode({
        "client_id": cid, "client_secret": secret,
        "refresh_token": refresh, "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(_TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["access_token"]


# ------------------------------------------------------------------ upload

def _manifest_title(manifest: dict, event: Any) -> str:
    """Build a YouTube title from the manifest + event."""
    title = (event.title if event else "") or manifest.get("event_id", "")
    # Strip outlet prefix ("Google News: ") for a cleaner title.
    if title.startswith("Google News: "):
        title = title[len("Google News: "):]
    return title[:100]                    # YouTube title limit


def _manifest_description(manifest: dict, event: Any) -> str:
    """Build a YouTube description with attribution + chapters."""
    parts = []
    sources = manifest.get("sources", [])
    if sources:
        parts.append("Sources: " + ", ".join(sources))
    parts.append(f"Event: {manifest.get('event_id', '')} "
                 f"revision {manifest.get('event_revision', '')}")
    chapters = manifest.get("youtube_chapters")
    if chapters:
        parts.append("\n" + chapters)
    is_short = (manifest.get("video", {}).get("height", 0) >
                manifest.get("video", {}).get("width", 0))
    if is_short:
        parts.append("\n#Shorts")
    parts.append("\nAutonomous newsroom — local-first crisis intelligence. "
                 "Live broadcast: https://www.youtube.com/@yourchannel")
    return "\n".join(parts)[:5000]       # YouTube description limit


def reviewed_metadata(conn: sqlite3.Connection, candidate: dict,
                      draft_path: Path) -> dict:
    """Bind operator-reviewed copy to one asset, one revision and its facts."""
    from ..storage import repositories as repo

    draft = json.loads(draft_path.read_text())
    manifest = candidate["manifest"]
    event_id = candidate["event_id"]
    if (draft.get("asset_id") != candidate["asset_id"] or
            draft.get("event_id") != event_id or
            draft.get("revision") != candidate["revision"] or
            draft.get("video_sha256") != manifest["video"]["hash"] or
            draft.get("privacy") != "private"):
        raise ValueError("Reviewed draft does not match this private video asset")
    title, description = draft.get("title"), draft.get("description")
    tags = draft.get("tags")
    if (not isinstance(title, str) or not title.strip() or len(title) > 100 or
            not isinstance(description, str) or not description.strip() or
            len(description) > 5000 or not isinstance(tags, list) or
            not tags or any(not isinstance(tag, str) or not tag.strip()
                            or len(tag) > 50 for tag in tags)):
        raise ValueError("Reviewed draft has invalid YouTube title or description")
    source = draft.get("source_url")
    urls = {f.value_display for f in repo.current_facts(conn, event_id)
            if f.field in ("source_url", "report_url", "eonet_link")}
    if (not source or source not in urls or source not in description or
            "https://www.youtube.com/@yourchannel" in description):
        raise ValueError("Reviewed draft must cite a current, linked source")
    return {"title": title, "description": description,
            "privacy": "private", "tags": tags}


def _upload_status(session_url: str, size: int, token: str) -> tuple[str, int]:
    """Return (confirmed video ID or '', next byte offset).

    Never start a second session if a saved one cannot be reconciled.
    """
    req = urllib.request.Request(
        session_url, data=b"", method="PUT",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Length": "0", "Content-Range": f"bytes */{size}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status in (200, 201):
                video_id = json.loads(resp.read()).get("id")
                if video_id:
                    return video_id, size
            raise RuntimeError("Upload completed without a confirmed video ID")
    except urllib.error.HTTPError as exc:
        if exc.code != 308:
            raise RuntimeError(f"Cannot reconcile upload session (HTTP {exc.code})") from None
        value = exc.headers.get("Range")
        if not value:
            return "", 0
        match = re.fullmatch(r"bytes=0-(\d+)", value)
        if not match or int(match.group(1)) >= size:
            raise RuntimeError("Invalid upload session range") from None
        return "", int(match.group(1)) + 1


def upload_video(manifest: dict, event: Any, token: str,
                 *, asset_id: str | None = None, session_dir: Path | None = None,
                 dry_run: bool = False,
                 reviewed: dict | None = None) -> dict:
    """Upload or reconcile an existing session; never blindly retry a PUT."""
    video_path = Path(manifest["video"]["path"])
    if not video_path.exists():
        return {"ok": False, "error": f"video file missing: {video_path}"}
    if reviewed is None and not dry_run:
        raise ValueError("Live uploads require reviewed metadata")
    title = reviewed["title"] if reviewed else _manifest_title(manifest, event)
    description = (reviewed["description"] if reviewed else
                   _manifest_description(manifest, event))
    is_short = (manifest.get("video", {}).get("height", 0) >
                manifest.get("video", {}).get("width", 0))
    tags = (reviewed["tags"] if reviewed else
            ["news", "autonomous"] + (["shorts"] if is_short else []))
    metadata = {
        "snippet": {
            "title": title, "description": description, "tags": tags,
            "categoryId": "25",           # News & Politics
        },
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
    }
    if dry_run:
        log_event(log, 20, "dry-run upload", title=title,
                  size_mb=video_path.stat().st_size / 1e6)
        return {"ok": True, "dry_run": True, "title": title}
    if not asset_id:
        raise ValueError("Asset ID required for crash-safe upload")
    try:
        # Keep one file descriptor open: atomic re-renders cannot swap video
        # bytes between chunks even if the output pathname is replaced.
        with video_path.open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            digest = manifest["video"].get("hash")
            if not digest or size == 0:
                raise RuntimeError("Missing video hash or empty video")
            if hashlib.file_digest(source, "sha256").hexdigest() != digest:
                raise RuntimeError("Video changed since render")
            with locked_session(asset_id, session_dir) as journal:
                state = load_session(journal)
                if state:
                    if state.get("hash") != digest or state.get("size") != size:
                        raise RuntimeError("Session video mismatch; reconcile manually")
                    if state.get("video_id"):
                        return {"ok": True, "video_id": state["video_id"], "title": title}
                    session_url = state["session_url"]
                    if not session_url_ok(session_url):
                        raise RuntimeError("Invalid saved upload session URL")
                else:
                    init_req = urllib.request.Request(
                        f"{_UPLOAD_URL}?uploadType=resumable&part=snippet,status",
                        data=json.dumps(metadata).encode(), method="POST",
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json",
                                 "X-Upload-Content-Type": "video/mp4",
                                 "X-Upload-Content-Length": str(size)})
                    with urllib.request.urlopen(init_req, timeout=30) as resp:
                        session_url = resp.headers.get("Location", "")
                    if not session_url_ok(session_url):
                        raise RuntimeError("Invalid upload session URL")
                    state = {"session_url": session_url, "hash": digest, "size": size}
                    save_session(journal, state)  # Before the first video byte.
                for _ in range(10000):
                    video_id, offset = _upload_status(session_url, size, token)
                    if video_id:
                        state["video_id"] = video_id
                        save_session(journal, state)  # Before updating the DB.
                        return {"ok": True, "video_id": video_id, "title": title}
                    source.seek(offset)
                    chunk = source.read(_CHUNK)
                    if not chunk:
                        raise RuntimeError("No remaining bytes but no video ID")
                    end = offset + len(chunk) - 1
                    req = urllib.request.Request(
                        session_url, data=chunk, method="PUT",
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Range": f"bytes {offset}-{end}/{size}",
                                 "Content-Length": str(len(chunk))})
                    try:
                        with urllib.request.urlopen(req, timeout=120) as resp:
                            if resp.status in (200, 201):
                                video_id = json.loads(resp.read()).get("id")
                                if video_id:
                                    state["video_id"] = video_id
                                    save_session(journal, state)
                                    return {"ok": True, "video_id": video_id, "title": title}
                                raise RuntimeError("Completed without a video ID")
                            if resp.status != 308:
                                raise RuntimeError(f"Unexpected response ({resp.status})")
                    except urllib.error.HTTPError as exc:
                        if exc.code != 308:
                            raise RuntimeError(f"Upload failed (HTTP {exc.code})") from None
                    # Query the server rather than assuming a chunk landed.
                raise RuntimeError("Upload exceeded safe retry limit")
    except (OSError, RuntimeError, KeyError, ValueError):
        # Never log a session URL or access token from a network exception.
        log_event(log, 30, "upload paused; session retained", asset_id=asset_id)
        return {"ok": False, "error": "Upload paused; retry to reconcile saved session"}


# ------------------------------------------------------------------ policy

def upload_eligible(conn: sqlite3.Connection, settings: Settings,
                    *, max_per_day: int = 100,
                    asset_id: str | None = None) -> list[dict]:
    """Select gate-passed assets eligible for YouTube upload.

    Rules:
    - publication_gate.verdict == "pass" in the manifest
    - not already uploaded (asset row has no youtube_id)
    - not air_blocked or archived
    - within the daily cap
    """
    from ..paths import output_dir
    from ..storage import repositories as repo
    daily_cap = min(100, max(0, int(settings.get(
        "publishing.youtube.max_per_day", 100))))
    used = conn.execute(
        """SELECT COUNT(*) FROM assets
           WHERE substr(json_extract(meta_json, '$.youtube_uploaded_at'), 1, 10)
                 = strftime('%Y-%m-%d', 'now')"""
    ).fetchone()[0]
    cap = min(max(0, max_per_day), max(0, daily_cap - used))
    if not cap:
        return []
    rows = conn.execute(
        """SELECT a.asset_id, a.event_id, a.revision, a.path, a.sha256,
                  a.meta_json
           FROM assets a
           JOIN events e ON e.event_id = a.event_id
           WHERE a.kind = 'video'
             AND a.revision = e.current_revision
             AND e.status NOT IN ('cancelled')
             AND e.event_type != 'weather_alert'
             AND COALESCE(json_extract(a.meta_json, '$.archived'), 0)=0
             AND COALESCE(json_extract(a.meta_json, '$.air_blocked'), 0)=0
             AND COALESCE(json_extract(a.meta_json, '$.youtube_id'), '')=''
             AND NOT EXISTS (
               SELECT 1 FROM assets prior WHERE prior.event_id=a.event_id
                 AND json_extract(prior.meta_json, '$.youtube_id') IS NOT NULL
             )
             AND NOT EXISTS (
               SELECT 1 FROM observations o WHERE o.event_id=a.event_id
                 AND (o.feed_url LIKE 'fixture:%'
                      OR o.feed_url LIKE 'file:%')
             )
           ORDER BY e.impact_score DESC, a.created_at DESC"""
    ).fetchall()
    out = []
    seen = set()
    for r in rows:
        if asset_id is not None and r["asset_id"] != asset_id:
            continue
        if r["event_id"] in seen or "sim" in r["event_id"].lower():
            continue
        seen.add(r["event_id"])
        manifest_path = (output_dir() / "manifests" /
                        f"{r['event_id'].replace(':', '_')}_r{r['revision']}.json")
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except (ValueError, OSError):
            continue
        if manifest.get("publication_gate", {}).get("verdict") != "pass":
            continue
        video = manifest.get("video", {})
        path = Path(r["path"])
        if (manifest.get("dry_run") or
                manifest.get("event_id") != r["event_id"] or
                manifest.get("event_revision") != r["revision"] or
                video.get("path") != r["path"] or not path.is_file() or
                not video.get("hash") or video["hash"] != r["sha256"]):
            continue
        with path.open("rb") as fh:
            digest = hashlib.file_digest(fh, "sha256").hexdigest()
        if digest != video["hash"]:
            continue
        out.append({
            "asset_id": r["asset_id"], "event_id": r["event_id"],
            "revision": r["revision"], "path": r["path"],
            "manifest_path": str(manifest_path),
            "manifest": manifest,
        })
        if len(out) >= cap:
            break
    return out


# ------------------------------------------------------------------ playlist

def select_best_of_day(conn: sqlite3.Connection,
                       settings: Settings, top_n: int = 10) -> list[dict]:
    """Select the most engaging stories of the last 24h for a playlist.

    Engagement proxy: impact_score × (1 + log(air_count)). The highest-
    impact story that aired the most wins; a FLASH that aired once still
    beats a NORMAL that aired 50 times.
    """
    rows = conn.execute(
        """SELECT e.event_id, e.title, e.impact_level, e.impact_score,
                  e.event_type, COUNT(a.air_id) AS air_count
           FROM events e
           JOIN air_log a ON a.event_id = e.event_id
           WHERE a.started_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 day')
             AND a.kind != 'replay'
           GROUP BY e.event_id
           ORDER BY e.impact_score * (1 + CAST(air_count AS REAL) / 10) DESC
           LIMIT ?""",
        (top_n,)
    ).fetchall()
    return [dict(r) for r in rows]


def create_playlist(title: str, description: str, token: str,
                    *, dry_run: bool = False) -> dict:
    """Create a YouTube playlist. Returns {playlist_id, dry_run}."""
    if dry_run:
        return {"ok": True, "dry_run": True, "title": title}
    body = json.dumps({
        "snippet": {"title": title[:100], "description": description[:5000]},
        "status": {"privacyStatus": "public"},
    }).encode()
    url = f"{_API_BASE}/playlists?part=snippet,status"
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            return {"ok": True, "playlist_id": body.get("id", ""),
                    "title": title}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": redact_error(e)}


def add_to_playlist(playlist_id: str, video_id: str, token: str,
                    *, dry_run: bool = False) -> dict:
    """Add a video to a playlist."""
    if dry_run:
        return {"ok": True, "dry_run": True, "video_id": video_id}
    body = json.dumps({
        "snippet": {"playlistId": playlist_id,
                     "resourceId": {"kind": "youtube#video",
                                    "videoId": video_id}},
    }).encode()
    url = f"{_API_BASE}/playlistItems?part=snippet"
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"ok": True, "video_id": video_id}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": redact_error(e)}
