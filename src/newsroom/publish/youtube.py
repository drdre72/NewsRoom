"""YouTube Data API v3 — OAuth device flow, resumable upload, playlists.

No Google client library: raw HTTP via urllib, same as every adapter.
The LLM never decides what uploads — the publication gate is
deterministic, and `publishing.automatic` is the master switch (§66).

OAuth: device flow → user visits a URL, enters a code, we poll until
authorized, then store the refresh token in .env as
NEWSROOM_YT_REFRESH_TOKEN. Access tokens are minted per upload from
the refresh token (1-hour lifetime).

Upload: resumable videos.insert — initiate session, chunk the MP4,
finalize. Title/description/chapters come from the render manifest.
Shorts (vertical 1080x1920) get `#Shorts` in the description.

Quota (June 2026): videos.insert has its own bucket — 100 uploads/day
at 1 unit each, independent of the 10,000-unit shared pool.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

from ..config import Settings
from ..logging import get_logger, log_event
from ..security import redact_error

log = get_logger("youtube")

# Google OAuth endpoints (public, documented).
_DEVICE_URL = "https://oauth2.googleapis.com/device/code"
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


def device_auth_url() -> dict[str, str]:
    """Initiate the OAuth device flow. Returns the URL the user visits
    and the code they enter. Caller polls exchange_device_code()."""
    cid, _ = _client_creds()
    data = urllib.parse.urlencode({
        "client_id": cid, "scope": _SCOPE,
    }).encode()
    req = urllib.request.Request(_DEVICE_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    return {
        "user_url": body["verification_url"],
        "user_code": body["user_code"],
        "device_code": body["device_code"],
        "expires_in": str(body["expires_in"]),
        "interval": str(body["interval"]),
    }


def exchange_device_code(device_code: str, interval: int = 5,
                         timeout: int = 300) -> str | None:
    """Poll the token endpoint until the user authorizes or it expires.
    Returns the refresh token, or None on timeout."""
    cid, secret = _client_creds()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(interval)
        data = urllib.parse.urlencode({
            "client_id": cid, "client_secret": secret,
            "device_code": device_code, "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }).encode()
        req = urllib.request.Request(_TOKEN_URL, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read())
            return body.get("refresh_token")
        except urllib.error.HTTPError as e:
            body = json.loads(e.read())
            err = body.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err == "expired_token":
                log_event(log, 40, "device code expired")
                return None
            log_event(log, 40, "oauth error", error=redact_error(e))
            return None
    return None


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


def upload_video(manifest: dict, event: Any, token: str,
                 *, dry_run: bool = False, privacy: str = "public") -> dict:
    """Resumable upload of one video. Returns {video_id, dry_run}.

    Steps: initiate session → chunked PUT → finalize. The MP4 path
    comes from manifest['video']['path'].
    """
    video_path = Path(manifest["video"]["path"])
    if not video_path.exists():
        return {"ok": False, "error": f"video file missing: {video_path}"}
    title = _manifest_title(manifest, event)
    description = _manifest_description(manifest, event)
    is_short = (manifest.get("video", {}).get("height", 0) >
                manifest.get("video", {}).get("width", 0))
    tags = ["news", "breaking", "autonomous"] + (
        ["shorts", "#shorts"] if is_short else [])
    metadata = {
        "snippet": {
            "title": title, "description": description, "tags": tags,
            "categoryId": "25",           # News & Politics
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    if dry_run:
        log_event(log, 20, "dry-run upload", title=title,
                  size_mb=video_path.stat().st_size / 1e6)
        return {"ok": True, "dry_run": True, "title": title}

    # Initiate resumable session.
    size = video_path.stat().st_size
    init_body = json.dumps(metadata).encode()
    init_url = (f"{_UPLOAD_URL}?uploadType=resumable&part=snippet,status")
    init_req = urllib.request.Request(
        init_url, data=init_body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "X-Upload-Content-Type": "video/mp4",
                 "X-Upload-Content-Length": str(size)})
    try:
        with urllib.request.urlopen(init_req, timeout=30) as resp:
            session_url = resp.headers["Location"]
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": redact_error(e)}

    # Chunked upload.
    with video_path.open("rb") as fh:
        offset = 0
        while offset < size:
            chunk = fh.read(_CHUNK)
            chunk_size = len(chunk)
            end = offset + chunk_size - 1
            put_req = urllib.request.Request(
                session_url, data=chunk, method="PUT",
                headers={"Content-Range": f"bytes {offset}-{end}/{size}",
                         "Content-Length": str(chunk_size)})
            try:
                with urllib.request.urlopen(put_req, timeout=120) as resp:
                    if resp.status == 200:
                        body = json.loads(resp.read())
                        vid = body.get("id", "")
                        log_event(log, 20, "upload complete",
                                  video_id=vid, title=title)
                        return {"ok": True, "video_id": vid, "title": title}
            except urllib.error.HTTPError as e:
                if e.code == 308:
                    # Partial — server tells us where to resume.
                    rng = e.headers.get("Range", f"bytes=0-{end}")
                    offset = int(rng.split("-")[-1]) + 1
                    continue
                return {"ok": False, "error": redact_error(e)}
            offset += chunk_size
    return {"ok": False, "error": "upload ended without confirmation"}


# ------------------------------------------------------------------ policy

def upload_eligible(conn: sqlite3.Connection, settings: Settings,
                    *, max_per_day: int = 100) -> list[dict]:
    """Select gate-passed assets eligible for YouTube upload.

    Rules:
    - publication_gate.verdict == "pass" in the manifest
    - not already uploaded (asset row has no youtube_id)
    - not air_blocked or archived
    - within the daily cap
    """
    from ..storage import repositories as repo
    cap = int(settings.get("publishing.youtube.max_per_day", max_per_day))
    rows = conn.execute(
        """SELECT a.asset_id, a.event_id, a.revision, a.path, a.meta_json
           FROM assets a
           JOIN events e ON e.event_id = a.event_id
           WHERE a.kind = 'video'
             AND COALESCE(json_extract(a.meta_json, '$.air_blocked'), 0)=0
             AND COALESCE(json_extract(a.meta_json, '$.archived_to'), '')=''
             AND (a.meta_json NOT LIKE '%"youtube_id"%'
                  OR a.meta_json IS NULL)
           ORDER BY e.impact_score DESC LIMIT ?""",
        (cap * 2,)
    ).fetchall()
    out = []
    for r in rows:
        manifest_path = (Path("output/manifests") /
                        f"{r['event_id'].replace(':', '_')}_r{r['revision']}.json")
        if not manifest_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("publication_gate", {}).get("verdict") != "pass":
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
