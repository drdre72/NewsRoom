"""Rehydration recipes — media retired to pure instruction form (§44, §52).

Policy: a video becomes eligible for dehydration once it has been
broadcast-irrelevant for `storage.media_retention_days` (default 7) —
last aired beyond the window, superseded by a newer revision, or unable
to premiere. Dehydration writes a self-contained recipe (validated
script, fact snapshot, visual plan, render settings, voice, hashes),
VERIFIES it by actually re-rendering, and only then removes the MP4 and
archives its asset row so it leaves air rotation automatically.

`python main.py restore <recipe|asset|event id>` rebuilds the video with
no manual steps. Deletion is never automatic: `dehydrate` runs as a plan
unless execute=True, and `storage.auto_dehydrate` stays off until the
operator approves it.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..logging import get_logger, log_event
from ..models.story import Script
from ..paths import cache_dir, resolve
from . import repositories as repo
from .database import tx

log = get_logger("recipes")

RECIPE_VERSION = 1


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_id(event_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", event_id)


def recipes_dir(settings: Settings) -> Path:
    d = resolve(settings.get("storage.recipes_dir", "output/recipes"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta(asset: sqlite3.Row) -> dict:
    try:
        return json.loads(asset["meta_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _manifest_for(conn: sqlite3.Connection, event_id: str, revision: int,
                  fmt: str) -> dict | None:
    """Newest manifest file for this event+revision+format, if readable."""
    rows = conn.execute(
        "SELECT * FROM assets WHERE event_id=? AND revision=?"
        " AND kind='manifest' ORDER BY created_at DESC",
        (event_id, revision),
    ).fetchall()
    for row in rows:
        if fmt == "longform" and "longform" not in row["path"]:
            continue
        if fmt == "short" and "longform" in row["path"]:
            continue
        try:
            return json.loads(Path(row["path"]).read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return None


# --------------------------------------------------------------------- build

def build_recipe(conn: sqlite3.Connection, settings: Settings,
                 asset: sqlite3.Row) -> dict:
    """Assemble the self-contained rebuild instruction for one video asset."""
    event_id, rev = asset["event_id"], asset["revision"]
    fmt = _meta(asset).get("format", "short")
    event = repo.get_event(conn, event_id)
    if not event:
        raise ValueError(f"event {event_id} missing; cannot build recipe")

    stories = repo.stories_for_render(conn, event_id, rev)
    story = None
    for row in stories:
        plan = json.loads(row["plan_json"] or "{}")
        is_explainer = plan.get("story_type") == "explainer"
        if (fmt == "longform") == is_explainer:
            story = row
            break
    if not story:
        raise ValueError(
            f"no validated {fmt} story for {event_id} r{rev}; "
            "refusing to write an incomplete recipe")
    script_dict = json.loads(story["script_json"])
    plan_dict = json.loads(story["plan_json"] or "{}")

    manifest = _manifest_for(conn, event_id, rev, fmt) or {}
    script = Script.from_dict(script_dict)

    # Visual plan: prefer the exact plan recorded at render time; older
    # manifests predate that field, so freeze a plan rebuilt now (from the
    # current fact state — the recipe documents how to rebuild *today*).
    w = int(manifest.get("video", {}).get("width", 0)) or int(
        settings.get(f"video.{'longform' if fmt == 'longform' else 'shorts'}",
                    {}).get("width", 1080))
    h = int(manifest.get("video", {}).get("height", 0)) or int(
        settings.get(f"video.{'longform' if fmt == 'longform' else 'shorts'}",
                    {}).get("height", 1920))
    fps = int(manifest.get("video", {}).get("fps", 30))
    if manifest.get("visual_plan"):
        vplan = manifest["visual_plan"]
    else:
        from ..visuals.planner import plan_visuals
        vp = plan_visuals(conn, event, script, w, h, fps)
        vp.revision = rev
        vplan = vp.to_dict()

    facts = [asdict(f) for f in repo.all_facts(conn, event_id)]
    video_path = asset["path"]
    exists = Path(video_path).exists()
    event_dict = event.to_dict()
    event_dict["current_revision"] = rev   # pin the rendered revision
    recipe = {
        "recipe_version": RECIPE_VERSION,
        "recipe_id": repo.new_id("recipe"),
        "asset_id": asset["asset_id"],
        "event_id": event_id,
        "revision": rev,
        "format": fmt,
        "created_at": repo.utcnow(),
        "event": event_dict,
        "story_id": story["story_id"],
        "generator": story["generator"],
        "script": script_dict,
        "plan": plan_dict,
        "facts": facts,
        "fact_ids": manifest.get("fact_ids")
                    or [f["fact_id"] for f in facts
                        if f["status"] == "current"],
        "visual_plan": vplan,
        "captions": manifest.get("captions"),
        "render": manifest.get("render") or {
            "title_s": 3.5, "tail_s": 4.0, "gap_s": 0.35,
            "mid_min_s": 3.0 if fmt == "longform" else 2.0,
            "target_floor_s": 8.0 if fmt == "longform" else 4.0,
            "width": w, "height": h, "fps": fps,
        },
        "context": {
            "level": event.impact_level,
            "event_label": event.event_type.replace("_", " "),
            "source_line": ("SOURCE: "
                            + (event.source_ids[0]
                               if event.source_ids else "unknown")),
            "updated": f"UPDATED {event.last_updated[11:19]} UTC",
        },
        "voice": manifest.get("voice") or manifest.get("tts_model"),
        "renderer": manifest.get("renderer"),
        "sources": manifest.get("sources") or event.source_ids,
        "video": {
            "path": video_path,
            "sha256": asset["sha256"] or (_sha256(Path(video_path))
                                          if exists else None),
            "bytes": Path(video_path).stat().st_size if exists else 0,
        },
        "gate_verdict": (manifest.get("publication_gate") or {}).get("verdict"),
        "air": repo.air_stats(conn, event_id, rev),
    }
    recipe["render"]["width"] = w
    recipe["render"]["height"] = h
    recipe["render"]["fps"] = fps
    return recipe


def write_recipe_file(settings: Settings, recipe: dict) -> Path:
    fmt = recipe["format"]
    suffix = "_longform" if fmt == "longform" else ""
    name = (f"{_safe_id(recipe['event_id'])}_r{recipe['revision']}"
            f"{suffix}.recipe.json")
    path = recipes_dir(settings) / name
    path.write_text(json.dumps(recipe, indent=2))
    return path


# -------------------------------------------------------------- eligibility

def effective_retention_days(settings: Settings) -> float:
    """7 days normally; tighter under disk pressure — media converts
    sooner when free space drops below 2x the render-stop threshold."""
    from .retention import disk_health
    base = float(settings.get("storage.media_retention_days", 7))
    free = disk_health(settings)["free_gb"]
    minimum = float(settings.get("storage.min_free_gb", 10))
    if free < 2 * minimum:
        return float(settings.get("storage.pressure_retention_days", 2))
    return base


def evaluate_asset(conn: sqlite3.Connection, settings: Settings,
                   asset: sqlite3.Row, now: float | None = None) -> dict:
    """Classify one video asset: protected | eligible | dehydrated |
    missing_file — with the reason and the bytes at stake."""
    now = time.time() if now is None else now
    meta = _meta(asset)
    path = Path(asset["path"])
    result = {"asset_id": asset["asset_id"], "event_id": asset["event_id"],
              "revision": asset["revision"], "format": meta.get("format",
                                                              "short"),
              "path": asset["path"], "archived": bool(meta.get("archived")),
              "bytes": path.stat().st_size if path.exists() else 0}

    if meta.get("archived"):
        return result | {"status": "dehydrated",
                         "reason": "already converted to recipe"}
    if not path.exists():
        return result | {"status": "missing_file",
                         "reason": "file gone but asset row not archived"}

    eff_days = effective_retention_days(settings)
    window_s = eff_days * 86400
    event = repo.get_event(conn, asset["event_id"])

    latest = repo.latest_air(conn)
    if latest and latest["asset_path"] == asset["path"]:
        aired_at = _parse_ts(latest["started_at"])
        # On-air = aired moments ago, not merely "the most recent ever".
        if aired_at and now - aired_at < 600:
            return result | {"status": "protected", "reason": "on_air"}
    if repo.pending_job_for(conn, asset["event_id"], asset["revision"]):
        return result | {"status": "protected", "reason": "job_pending"}
    if repo.pending_publication_for(conn, asset["event_id"]):
        return result | {"status": "protected",
                         "reason": "awaiting_publication"}

    stats = repo.air_stats(conn, asset["event_id"], asset["revision"])
    result["air"] = stats
    if stats["count"]:
        last = _parse_ts(stats["last_at"])
        if last and now - last > window_s:
            return result | {"status": "eligible",
                             "reason": "retired",
                             "last_air_age_days": round(
                                 (now - last) / 86400, 1)}
        return result | {"status": "protected",
                         "reason": "recently_aired"}

    # Never aired.
    superseded = event and event.current_revision != asset["revision"]
    if superseded:
        return result | {"status": "eligible", "reason": "stale_unaired"}
    min_conf = float(settings.get("auto_render.min_confidence", 0.70))
    if event and event.confidence_score >= min_conf:
        return result | {"status": "protected",
                         "reason": "awaiting_premiere"}
    mtime = path.stat().st_mtime
    if now - mtime > window_s:
        return result | {"status": "eligible",
                         "reason": "low_confidence_unaired"}
    return result | {"status": "protected", "reason": "unairable_recent"}


def retention_report(conn: sqlite3.Connection,
                     settings: Settings) -> dict:
    """Eligibility ledger — shown to the operator before anything deletes."""
    from .retention import disk_health
    now = time.time()
    groups: dict[str, list[dict]] = {}
    for asset in repo.video_assets(conn):
        ev = evaluate_asset(conn, settings, asset, now)
        groups.setdefault(ev["status"], []).append(ev)
    eligible_bytes = sum(e["bytes"] for e in groups.get("eligible", []))
    return {
        "effective_retention_days": effective_retention_days(settings),
        "disk": disk_health(settings),
        "counts": {k: len(v) for k, v in groups.items()},
        "eligible_bytes": eligible_bytes,
        "eligible": [
            {"asset_id": e["asset_id"], "event_id": e["event_id"],
             "revision": e["revision"], "format": e["format"],
             "reason": e["reason"], "bytes": e["bytes"],
             "path": e["path"]}
            for e in groups.get("eligible", [])],
        "protected": [
            {"asset_id": e["asset_id"], "event_id": e["event_id"],
             "reason": e["reason"], "bytes": e["bytes"]}
            for e in groups.get("protected", [])],
        "dehydrated": len(groups.get("dehydrated", [])),
        "missing_file": len(groups.get("missing_file", [])),
        "recipes": {
            s: len(repo.list_recipes(conn, s))
            for s in ("written", "verified", "dehydrated", "verify_failed",
                      "restored")},
    }


# ---------------------------------------------------------------- dehydrate

def verify_recipe(conn: sqlite3.Connection, settings: Settings,
                  recipe_row: sqlite3.Row) -> dict:
    """Prove a recipe restores BEFORE its MP4 becomes removable (§52).
    Re-renders to scratch and ffprobes the result; touches nothing else."""
    from ..render import render_from_recipe

    path = Path(recipe_row["path"])
    if not path.exists():
        return {"ok": False, "issues": ["recipe file missing"]}
    if _sha256(path) != recipe_row["recipe_sha256"]:
        return {"ok": False, "issues": ["recipe sha256 mismatch — "
                                        "file changed since it was written"]}
    recipe = json.loads(path.read_text())
    # Verification media goes to an explicit scratch dir — never anywhere
    # near real output. Cleanup removes only that dir, nothing it sniffs.
    scratch = cache_dir() / "renders" / f"verify_{recipe_row['recipe_id']}"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        manifest = render_from_recipe(conn, settings, recipe,
                                    out_path=scratch / "verify.mp4",
                                    persist=False)
    except Exception as exc:  # noqa: BLE001 — failure = do not delete
        shutil.rmtree(scratch, ignore_errors=True)
        return {"ok": False, "issues": [f"restore render failed: {exc}"]}
    check = manifest.get("verify", {})
    ok = bool(check.get("ok")) and not manifest.get("dry_run")
    report = {"ok": ok, "issues": check.get("issues", []),
              "duration_s": check.get("duration_s"),
              "video": manifest["video"]["path"]}
    shutil.rmtree(scratch, ignore_errors=True)
    return report


def dehydrate(conn: sqlite3.Connection, settings: Settings, *,
              execute: bool = False, verify: bool = True,
              budget: int | None = None,
              asset_ids: list[str] | None = None) -> dict:
    """Convert eligible video assets to verified recipes.

    execute=False (and the CLI default) returns the plan only — nothing
    is written, archived, or unlinked.
    """
    now = time.time()
    plan, done, skipped = [], [], []
    freed = 0
    assets = repo.video_assets(conn)
    for asset in assets:
        if asset_ids and asset["asset_id"] not in asset_ids:
            continue
        ev = evaluate_asset(conn, settings, asset, now)
        # Reap strays: archived row + leftover file (crash mid-dehydrate).
        if ev["status"] == "dehydrated" and Path(ev["path"]).exists():
            if execute:
                Path(ev["path"]).unlink(missing_ok=True)
            ev = ev | {"status": "eligible", "reason": "reap_stray"}
        if ev["status"] != "eligible":
            skipped.append(ev)
            continue
        existing = repo.recipe_for_asset(conn, asset["asset_id"])
        if existing and existing["status"] == "restored":
            # A restored video re-entered rotation as a new asset row;
            # never let the old recipe delete it again.
            skipped.append(ev | {"status": "skipped",
                                 "reason": "recipe was restored"})
            continue
        if existing and existing["status"] == "dehydrated":
            skipped.append(ev | {"status": "skipped",
                                 "reason": "already dehydrated"})
            continue
        if budget is not None and len(plan) >= budget:
            break
        plan.append(ev)
        if not execute:
            continue
        try:
            if existing:
                # written/verify_failed recipes retry: verify as-is,
                # then finish the release if it now passes.
                row = existing
            else:
                recipe = build_recipe(conn, settings, asset)
                rpath = write_recipe_file(settings, recipe)
                recipe_row = {
                    "recipe_id": recipe["recipe_id"],
                    "asset_id": asset["asset_id"],
                    "event_id": ev["event_id"],
                    "revision": ev["revision"], "format": ev["format"],
                    "path": str(rpath), "video_path": ev["path"],
                    "video_sha256": recipe["video"]["sha256"],
                    "video_bytes": recipe["video"]["bytes"],
                    "recipe_sha256": _sha256(rpath),
                    "status": "written",
                    "last_air_at": (ev.get("air") or {}).get("last_at"),
                    "created_at": repo.utcnow(),
                }
                with tx(conn):
                    repo.insert_recipe(conn, recipe_row)
                row = repo.get_recipe(conn, recipe["recipe_id"])
            if verify:
                report = verify_recipe(conn, settings, row)
                if not report["ok"]:
                    with tx(conn):
                        repo.set_recipe_status(
                            conn, row["recipe_id"], "verify_failed",
                            verify_report_json=json.dumps(report))
                    ev = ev | {"status": "verify_failed",
                               "issues": report["issues"]}
                    done.append(ev)
                    continue
                with tx(conn):
                    repo.set_recipe_status(
                        conn, row["recipe_id"], "verified",
                        verified_at=repo.utcnow(),
                        verify_report_json=json.dumps(report))
            else:
                # Policy: an unverified recipe never releases its MP4.
                ev = ev | {"status": "recipe_written_unverified",
                           "recipe_id": row["recipe_id"]}
                done.append(ev)
                continue
            # Archive the asset row first: a crash after this commit can
            # never leave a live-row/missing-file pair (§75).
            with tx(conn):
                repo.patch_asset_meta(conn, asset["asset_id"], {
                    "archived": 1, "archived_at": repo.utcnow(),
                    "recipe_id": row["recipe_id"]})
            Path(ev["path"]).unlink(missing_ok=True)
            with tx(conn):
                repo.set_recipe_status(conn, row["recipe_id"], "dehydrated",
                                       dehydrated_at=repo.utcnow())
            freed += ev["bytes"]
            ev = ev | {"status": "dehydrated",
                       "recipe_id": row["recipe_id"]}
            done.append(ev)
            log_event(log, 20, "asset dehydrated",
                      asset_id=asset["asset_id"], event_id=ev["event_id"],
                      freed=ev["bytes"], recipe=row["path"])
        except Exception as exc:  # noqa: BLE001 — one bad asset skips, not aborts
            log_event(log, 40, "dehydrate failed",
                      asset_id=asset["asset_id"], error=str(exc))
            skipped.append(ev | {"status": "error", "error": str(exc)})
    return {"execute": execute, "verify": verify,
            "planned": len(plan), "done": done, "skipped": skipped,
            "freed_bytes": freed}


# ------------------------------------------------------------------- restore

def find_recipe(conn: sqlite3.Connection,
                ident: str) -> sqlite3.Row | None:
    """Resolve a recipe by recipe_id, asset_id, or event_id (latest)."""
    row = repo.get_recipe(conn, ident) or repo.recipe_for_asset(conn, ident)
    if row:
        return row
    rows = repo.recipes_for_event(conn, ident)
    return rows[-1] if rows else None


def restore(conn: sqlite3.Connection, settings: Settings,
            ident: str) -> dict:
    """Rebuild a retired video — `python main.py restore <id>` is the only
    step; the recipe carries everything else."""
    from ..render import render_from_recipe

    row = find_recipe(conn, ident)
    if not row:
        raise ValueError(f"no recipe for {ident!r} — is it dehydrated?")
    path = Path(row["path"])
    if not path.exists():
        raise RuntimeError(f"recipe file missing: {path}")
    if _sha256(path) != row["recipe_sha256"]:
        raise RuntimeError(f"recipe sha256 mismatch: {path}")
    recipe = json.loads(path.read_text())
    manifest = render_from_recipe(conn, settings, recipe)
    with tx(conn):
        repo.set_recipe_status(conn, row["recipe_id"], "restored",
                               restored_at=repo.utcnow())
        # The fresh asset is live; the retired one stays archived history.
        original = repo.get_asset(conn, row["asset_id"])
        if original and _meta(original).get("archived"):
            repo.patch_asset_meta(conn, row["asset_id"],
                                  {"restored_as": manifest["video"]["path"]})
    log_event(log, 20, "recipe restored", recipe_id=row["recipe_id"],
              video=manifest["video"]["path"],
              gate=manifest["publication_gate"]["verdict"])
    return manifest
