"""iCloud Drive archive for completed media masters (§44, §52).

Purpose: the recipe proves a video can be rebuilt; the archive preserves
the exact approved master for anything we might republish. iCloud is an
archive target only — never the live working disk, never the sole copy.

macOS syncs the folder asynchronously (and TripMode may hold it during
gaming) — so "archived" here means: copied into the local iCloud Drive
tree and sha256-verified against the original. The local master is never
removed by this module; deletion stays with the recipe policy. Copies
are sequential and bounded per run (`archive.max_gb_per_run`) so an
archive pass can't compete with renders or saturate upstream.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from ..config import Settings
from ..logging import get_logger, log_event
from ..paths import resolve
from . import repositories as repo
from .database import tx

log = get_logger("archive")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _meta(asset: sqlite3.Row) -> dict:
    try:
        return json.loads(asset["meta_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def icloud_dir(settings: Settings) -> Path | None:
    if not settings.get("archive.enabled", False):
        return None
    raw = settings.get(
        "archive.icloud_dir",
        "~/Library/Mobile Documents/com~apple~CloudDocs/newsroom")
    return Path(raw).expanduser()


def _dest_for(icloud: Path, asset: sqlite3.Row, meta: dict) -> Path:
    fmt = meta.get("format", "short")
    sub = "longform" if fmt == "longform" else "shorts"
    return icloud / sub / Path(asset["path"]).name


def archive_report(conn: sqlite3.Connection,
                   settings: Settings) -> dict:
    """What would archive — shown before any copy is scheduled."""
    icloud = icloud_dir(settings)
    pending, done, missing = [], [], []
    for asset in repo.video_assets(conn):
        meta = _meta(asset)
        if meta.get("archived"):
            continue  # dehydrated media is recipe-form, not archive stock
        if meta.get("archived_to"):
            done.append(asset["asset_id"])
            continue
        if not Path(asset["path"]).exists():
            missing.append(asset["asset_id"])
            continue
        pending.append({
            "asset_id": asset["asset_id"], "event_id": asset["event_id"],
            "path": asset["path"], "format": meta.get("format", "short"),
            "bytes": Path(asset["path"]).stat().st_size,
        })
    return {
        "enabled": icloud is not None,
        "icloud_dir": str(icloud) if icloud else None,
        "pending": pending,
        "pending_bytes": sum(p["bytes"] for p in pending),
        "already_archived": len(done),
        "missing_files": missing,
    }


def run_archive(conn: sqlite3.Connection, settings: Settings,
                max_gb: float | None = None) -> dict:
    """Copy pending masters to iCloud Drive, sha256-verified, budgeted.

    The asset row records `archived_to` only after the copied bytes match;
    an interrupted run simply retries on the next pass.
    """
    icloud = icloud_dir(settings)
    if not icloud:
        return {"ok": False, "reason": "archive.disabled",
                "copied": [], "copied_bytes": 0}
    (icloud / "shorts").mkdir(parents=True, exist_ok=True)
    (icloud / "longform").mkdir(parents=True, exist_ok=True)
    (icloud / "manifests").mkdir(parents=True, exist_ok=True)

    budget = float(max_gb if max_gb is not None else
                   settings.get("archive.max_gb_per_run", 5)) * (1024 ** 3)
    copied, failed, skipped = [], [], []
    total = 0
    for asset in repo.video_assets(conn):
        meta = _meta(asset)
        if meta.get("archived") or meta.get("archived_to"):
            continue
        src = Path(asset["path"])
        if not src.exists():
            continue
        size = src.stat().st_size
        if total + size > budget:
            skipped.append(asset["asset_id"])
            continue
        dest = _dest_for(icloud, asset, meta)
        try:
            # Verify the copy against the live file, not the recorded
            # hash — a same-path re-render can leave the row's sha stale.
            src_sha = _sha256(src)
            fields = {"archived_to": str(dest),
                      "archived_at": repo.utcnow()}
            if asset["sha256"] and asset["sha256"] != src_sha:
                fields["sha_corrected_from"] = asset["sha256"]
                log_event(log, 30, "recorded sha drifted from file",
                          asset_id=asset["asset_id"])
            if not (dest.exists() and _sha256(dest) == src_sha):
                shutil.copy2(src, dest)
                if _sha256(dest) != src_sha:
                    raise RuntimeError("copied sha256 mismatch")
            # Pair the manifest when one exists for this render.
            manifest_row = next(
                (r for r in repo.assets_for_event(conn, asset["event_id"])
                 if r["kind"] == "manifest"
                 and r["revision"] == asset["revision"]
                 and (("longform" in r["path"])
                      == (meta.get("format") == "longform"))), None)
            if manifest_row:
                msrc = Path(manifest_row["path"])
                if msrc.exists():
                    mdest = icloud / "manifests" / msrc.name
                    if not (mdest.exists()
                            and _sha256(mdest) == _sha256(msrc)):
                        shutil.copy2(msrc, mdest)
            with tx(conn):
                if fields.get("sha_corrected_from"):
                    repo.set_asset_sha(conn, asset["asset_id"], src_sha)
                repo.patch_asset_meta(conn, asset["asset_id"], fields)
            total += size
            copied.append({"asset_id": asset["asset_id"],
                           "dest": str(dest), "bytes": size})
            log_event(log, 20, "asset archived to icloud",
                      asset_id=asset["asset_id"], dest=dest.name,
                      bytes=size)
        except Exception as exc:  # noqa: BLE001 — one failure skips, not aborts
            log_event(log, 40, "archive copy failed",
                      asset_id=asset["asset_id"], error=str(exc))
            failed.append({"asset_id": asset["asset_id"],
                           "error": str(exc)})
    return {"ok": True, "icloud_dir": str(icloud),
            "copied": copied, "copied_bytes": total,
            "failed": failed, "deferred_by_budget": skipped}
