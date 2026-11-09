"""TikTok post desk — manual draft packages, no API upload.

TikTok's Content Posting API audit rejects tools whose purpose is
uploading to the developer's own account, and Direct Post requires an
audited client. The compliant path is: the newsroom renders a vertical
explainer and drafts a posting package (caption + checklist +
provenance); the operator reviews and posts it at tiktok.com/upload.

The operator IS the publisher — same publication gate, same restricted-
source exclusion, same fact-cited manifest provenance as the YouTube
path. `tiktok posted` records the resulting URL back into the asset
ledger so the audit trail closes.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any

from ..config import Settings
from ..logging import get_logger, log_event

log = get_logger("tiktok")

_CAPTION_LIMIT = 2200          # TikTok description field
_MAX_HASHTAGS = 5
_RESTRICTED_SOURCES = ("gnews_crime", "gnews_federal")

_TYPE_HASHTAG = {
    "earthquake": "#earthquake",
    "tropical_cyclone": "#hurricane",
    "wildfire": "#wildfire",
    "weather_alert": "#weather",
    "conflict_event": "#conflict",
    "economic_indicator": "#economy",
    "market_quote": "#markets",
    "disaster_declaration": "#fema",
    "cyber_advisory": "#cybersecurity",
    "news_report": "#worldnews",
    "crime_report": "#crime",
    "space_weather": "#spaceweather",
}


# ------------------------------------------------------------------ captions

def _clean_title(event: Any, manifest: dict) -> str:
    title = (event.title if event else "") or manifest.get("event_id", "")
    if title.startswith("Google News: "):
        title = title[len("Google News: "):]
    return title.strip()


def _source_domain(manifest: dict, facts) -> str:
    """Domain-only citation: TikTok captions can't carry clickable links,
    and pasting raw URLs reads as spam. Domain keeps provenance visible."""
    for f in facts:
        if f.field in ("source_url", "report_url", "eonet_link"):
            host = urllib.parse.urlsplit(str(f.value_display)).netloc
            if host:
                return host.removeprefix("www.")
    return ""


def _source_name(manifest: dict, facts) -> str:
    """Display name for the source: the outlet fact wins ('BBC World');
    otherwise prettify the adapter id ('fema' -> 'FEMA')."""
    for f in facts:
        if f.field == "outlet" and f.value_display:
            return str(f.value_display)
    src = (manifest.get("sources") or ["unknown"])[0].split(":", 1)[0]
    if len(src) <= 4:
        return src.upper()
    return src.replace("_", " ").title()


def _hashtags(event: Any) -> list[str]:
    tags = ["#news"]
    type_tag = _TYPE_HASHTAG.get(getattr(event, "event_type", ""))
    if type_tag:
        tags.append(type_tag)
    label = getattr(event, "location_label", "") or ""
    if label:
        # "FL, United States" -> tag the meaningful part, not the compound.
        part = max((p.strip() for p in label.split(",")), key=len,
                   default="")
        slug = re.sub(r"[^A-Za-z0-9]", "", part)[:24]
        if slug:
            tags.append("#" + slug)
    if getattr(event, "impact_level", "") in ("BREAKING", "FLASH"):
        tags.append("#breaking")
    tags.append("#worldnews")
    seen, out = set(), []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:_MAX_HASHTAGS]


def build_caption(manifest: dict, event: Any, facts,
                  settings: Settings) -> str:
    """Paste-ready TikTok description.

    First line is the hook (the attributed lede the validator already
    approved), then a source line with domain-only citation, the AI
    disclosure, the follow CTA, and bounded hashtags. Never includes a
    bare URL — TikTok descriptions render them as dead text.
    """
    lede = ""
    captions = manifest.get("captions") or []
    if captions:
        lede = str(captions[0][2]).strip()
    title = _clean_title(event, manifest)
    handle = settings.get("publishing.tiktok.handle", "") or ""

    domain = _source_domain(manifest, facts)
    source_line = "Source: " + _source_name(manifest, facts)
    if domain:
        source_line += f" ({domain})"

    blocks = [title]
    if (lede and lede.rstrip(".!?").lower() != title.rstrip(".!?").lower()):
        blocks.append(lede)
    blocks.append(source_line)
    blocks.append("AI-narrated autonomous newsroom")
    if handle:
        blocks.append(f"Follow {handle} for continuing coverage")
    caption = "\n\n".join(blocks)
    tag_line = " ".join(_hashtags(event))
    if len(caption) + len(tag_line) + 2 > _CAPTION_LIMIT:
        # Drop the lede before ever truncating mid-sentence.
        caption = "\n\n".join(blocks[:1] + blocks[2:])
    caption = f"{caption}\n\n{tag_line}"
    return caption[:_CAPTION_LIMIT]


# ------------------------------------------------------------------ checklist

def build_checklist(candidate: dict, event: Any, caption: str,
                    rewards_ok: bool, settings: Settings) -> str:
    """Step list for the human publisher — the AIGC disclosure toggle is
    mandatory for fully synthetic video."""
    manifest = candidate["manifest"]
    video = manifest.get("video", {})
    duration = video.get("duration_s")
    dur_s = f"{duration:.0f}s" if isinstance(duration, (int, float)) else "?"
    size_mb = (Path(candidate["path"]).stat().st_size / 1e6
               if Path(candidate["path"]).exists() else 0)
    handle = settings.get("publishing.tiktok.handle", "") or "@justsystemnews"
    event_id = candidate["event_id"]
    rewards = ("✅ over 60s — qualifies for Creator Rewards once the account "
               "is enrolled" if rewards_ok else
               "⚠️ under 60s — will not earn Creator Rewards; post anyway "
               "for reach if the story merits it")
    return f"""# TikTok posting checklist

**{_clean_title(event, manifest)}**

- Video: `video.mp4` — {dur_s}, {video.get('width')}x{video.get('height')}, {size_mb:.1f} MB
- Event: `{event_id}` revision {candidate['revision']}
- Gate: {manifest.get('publication_gate', {}).get('verdict', '?')}
- Rewards: {rewards}

## Post at tiktok.com/upload (logged in as {handle})

1. Drag `video.mp4` into the upload box.
2. Paste the full contents of `caption.txt` into the description.
3. Turn ON **"Disclose AI-generated content"** — required: this video
   is fully synthetic (AI voice + generated visuals).
4. Audience: **Everyone** (or pick a schedule slot).
5. Leave "Run a copyright check" on — wait for it to finish.
6. Post, then record the URL back into the ledger:

   ```
   python main.py tiktok posted {event_id} <video-url>
   ```
"""


# ------------------------------------------------------------------ policy

def _excluded_event_ids(conn: sqlite3.Connection) -> tuple[set, set]:
    """Restricted-source and fixture/sim-derived event ids.

    Fetched once per call: a NOT EXISTS subquery per row does a full
    observations scan per event — with no index on event_id that is
    O(events × observations) and never returns on a real ledger.
    """
    restricted = {r[0] for r in conn.execute(
        "SELECT DISTINCT event_id FROM event_sources"
        " WHERE source IN ('gnews_crime', 'gnews_federal')")}
    fixture = {r[0] for r in conn.execute(
        "SELECT DISTINCT event_id FROM observations"
        " WHERE feed_url LIKE 'fixture:%' OR feed_url LIKE 'file:%'")}
    return restricted, fixture


def _tiktok_manifest_path(event_id: str, revision: int, out_dir: Path) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", event_id)
    return out_dir / "manifests" / f"{safe_id}_r{revision}_tiktok.json"


def _verified_candidate(row, out_dir: Path) -> dict | None:
    """Manifest + hash verification, same standard as upload_eligible."""
    manifest_path = _tiktok_manifest_path(row["event_id"], row["revision"],
                                          out_dir)
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except (ValueError, OSError):
        return None
    if manifest.get("publication_gate", {}).get("verdict") != "pass":
        return None
    video = manifest.get("video", {})
    path = Path(row["path"])
    if (manifest.get("dry_run") or
            manifest.get("event_id") != row["event_id"] or
            manifest.get("event_revision") != row["revision"] or
            video.get("path") != row["path"] or not path.is_file() or
            not video.get("hash") or video["hash"] != row["sha256"]):
        return None
    with path.open("rb") as fh:
        if hashlib.file_digest(fh, "sha256").hexdigest() != video["hash"]:
            return None
    return {"asset_id": row["asset_id"], "event_id": row["event_id"],
            "revision": row["revision"], "path": row["path"],
            "manifest_path": str(manifest_path), "manifest": manifest}


def draft_candidates(conn: sqlite3.Connection, settings: Settings,
                     *, event_id: str | None = None,
                     enforce_daily_cap: bool = True) -> list[dict]:
    """Rendered, gate-passed tiktok assets awaiting a draft package.

    Mirrors upload_eligible: current revision only, no weather, no
    restricted sources, no fixture/sim events, hash-verified manifest.
    Excludes assets already drafted or already posted.
    """
    from ..paths import output_dir
    out_dir = output_dir()
    if enforce_daily_cap:
        cap = int(settings.get("publishing.tiktok.max_drafts_per_day", 3))
        used = conn.execute(
            """SELECT COUNT(*) FROM assets
               WHERE substr(json_extract(meta_json, '$.tiktok_draft_at'),
                            1, 10) = strftime('%Y-%m-%d', 'now')"""
        ).fetchone()[0]
        if used >= cap:
            return []
    restricted_ids, fixture_ids = _excluded_event_ids(conn)
    rows = conn.execute(
        """SELECT a.asset_id, a.event_id, a.revision, a.path, a.sha256,
                  a.meta_json
           FROM assets a
           JOIN events e ON e.event_id = a.event_id
           WHERE a.kind = 'video'
             AND json_extract(a.meta_json, '$.format') = 'tiktok'
             AND a.revision = e.current_revision
             AND e.status NOT IN ('cancelled')
             AND e.event_type != 'weather_alert'
             AND COALESCE(json_extract(a.meta_json, '$.archived'), 0)=0
             AND COALESCE(json_extract(a.meta_json, '$.air_blocked'), 0)=0
             AND COALESCE(json_extract(a.meta_json, '$.tiktok_url'), '')=''
             AND COALESCE(json_extract(a.meta_json, '$.tiktok_inbox_at'), '')=''
             AND COALESCE(json_extract(a.meta_json, '$.tiktok_draft_at'), '')=''
           ORDER BY e.impact_score DESC, a.created_at DESC"""
    ).fetchall()
    out, seen = [], set()
    for r in rows:
        if event_id is not None and r["event_id"] != event_id:
            continue
        if (r["event_id"] in seen or r["event_id"] in restricted_ids
                or r["event_id"] in fixture_ids
                or "sim" in r["event_id"].lower()):
            continue
        cand = _verified_candidate(r, out_dir)
        if cand is None:
            continue
        seen.add(r["event_id"])
        out.append(cand)
    return out


def list_candidates(conn: sqlite3.Connection, settings: Settings,
                    limit: int = 20) -> list[dict]:
    """Events worth a TikTok post, with pipeline status per event.

    Advisory surface for the operator: what could be rendered/drafted/
    posted next, ordered by impact. Confidence tier below 'corroborate'
    is excluded — a draft is still gated again by the render manifest.
    """
    from ..intelligence.confidence_engine import production_gate
    restricted_ids, fixture_ids = _excluded_event_ids(conn)
    rows = conn.execute(
        """SELECT e.event_id, e.title, e.event_type, e.impact_level,
                  e.impact_score, e.confidence_score, e.current_revision,
                  e.last_updated
           FROM events e
           WHERE e.status NOT IN ('cancelled')
             AND e.event_type != 'weather_alert'
           ORDER BY e.impact_score DESC, e.last_updated DESC
           LIMIT ?""", (limit * 4,)
    ).fetchall()
    out = []
    for r in rows:
        eid = r["event_id"]
        if ("sim" in eid.lower() or eid in restricted_ids
                or eid in fixture_ids):
            continue
        tier = production_gate(float(r["confidence_score"]), settings)
        if tier == "hold":
            continue
        metas = [json.loads(a["meta_json"]) for a in conn.execute(
            """SELECT meta_json FROM assets
               WHERE event_id=? AND kind='video'
                 AND json_extract(meta_json, '$.format')='tiktok'""",
            (eid,)).fetchall()]
        posted = any(m.get("tiktok_url") for m in metas)
        inboxed = any(m.get("tiktok_inbox_at") for m in metas)
        drafted = any(m.get("tiktok_draft_at") for m in metas)
        status = ("posted" if posted else "inbox" if inboxed
                  else "drafted" if drafted
                  else "rendered" if metas else "new")
        out.append({
            "event_id": eid, "title": r["title"], "type": r["event_type"],
            "impact": r["impact_level"], "impact_score": r["impact_score"],
            "confidence": r["confidence_score"], "tier": tier,
            "revision": r["current_revision"], "tiktok": status,
            "updated": r["last_updated"],
        })
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ package

def rewards_eligible(manifest: dict, settings: Settings) -> bool:
    """Creator Rewards counts only videos over one minute."""
    duration = manifest.get("video", {}).get("duration_s")
    if not isinstance(duration, (int, float)):
        return False
    floor = float(settings.get("video.tiktok.min_rewards_s", 61))
    return duration >= floor


def build_draft_package(conn: sqlite3.Connection, settings: Settings,
                        candidate: dict, dest_root: Path | None = None
                        ) -> dict:
    """Assemble output/tiktok/<slug>/ with everything the human posts."""
    from ..paths import output_dir
    from ..storage import repositories as repo
    event_id = candidate["event_id"]
    manifest = candidate["manifest"]
    event = repo.get_event(conn, event_id)
    facts = repo.current_facts(conn, event_id)

    title_slug = re.sub(r"[^A-Za-z0-9]+", "_",
                        _clean_title(event, manifest))[:40].strip("_")
    pkg_dir = (dest_root or output_dir() / "tiktok") / (
        f"{title_slug or 'post'}_r{candidate['revision']}")
    pkg_dir.mkdir(parents=True, exist_ok=True)

    video_dest = pkg_dir / "video.mp4"
    shutil.copyfile(candidate["path"], video_dest)

    caption = build_caption(manifest, event, facts, settings)
    (pkg_dir / "caption.txt").write_text(caption + "\n")

    rewards_ok = rewards_eligible(manifest, settings)
    (pkg_dir / "checklist.md").write_text(
        build_checklist(candidate, event, caption, rewards_ok, settings))

    drafted_at = repo.utcnow()
    draft = {
        "event_id": event_id, "revision": candidate["revision"],
        "asset_id": candidate["asset_id"],
        "video": {"path": str(video_dest),
                  "sha256": manifest["video"]["hash"],
                  "duration_s": manifest["video"].get("duration_s"),
                  "width": manifest["video"].get("width"),
                  "height": manifest["video"].get("height")},
        "caption": caption,
        "sources": manifest.get("sources", []),
        "fact_ids": manifest.get("fact_ids", []),
        "publication_gate": manifest.get("publication_gate", {}),
        "rewards_eligible": rewards_ok,
        "aigc_disclosure_required": True,
        "handle": settings.get("publishing.tiktok.handle", ""),
        "status": "awaiting_manual_post",
        "drafted_at": drafted_at,
    }
    (pkg_dir / "draft.json").write_text(json.dumps(draft, indent=2))
    repo.patch_asset_meta(conn, candidate["asset_id"],
                          {"tiktok_draft_at": drafted_at,
                           "tiktok_draft_dir": str(pkg_dir)})
    log_event(log, 20, "tiktok draft packaged", event_id=event_id,
              dir=str(pkg_dir))
    return {"dir": str(pkg_dir), "caption": caption,
            "rewards_eligible": rewards_ok, "draft": draft}


def mark_posted(conn: sqlite3.Connection, event_id: str, url: str) -> dict:
    """Record the posted TikTok URL on the newest tiktok video asset."""
    from ..storage import repositories as repo
    host = urllib.parse.urlsplit(url.strip()).netloc.lower()
    if not (host == "tiktok.com" or host.endswith(".tiktok.com")):
        raise ValueError("expected a tiktok.com URL")
    rows = conn.execute(
        """SELECT asset_id, meta_json FROM assets
           WHERE event_id=? AND kind='video'
             AND json_extract(meta_json, '$.format')='tiktok'
           ORDER BY created_at DESC""", (event_id,)).fetchall()
    if not rows:
        raise ValueError(f"no tiktok render for {event_id}")
    posted_at = repo.utcnow()
    for r in rows:
        repo.patch_asset_meta(conn, r["asset_id"],
                              {"tiktok_url": url.strip(),
                               "tiktok_posted_at": posted_at})
    log_event(log, 20, "tiktok post recorded", event_id=event_id)
    return {"event_id": event_id, "url": url.strip(),
            "assets": len(rows)}
