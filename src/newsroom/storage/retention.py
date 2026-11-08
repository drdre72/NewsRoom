"""Read-only disk audit and a free-space guard for continuous operation.

Cleanup is deliberately not automatic: old evidence and media cannot be
deleted until the operator has approved an explicit retention policy.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from ..config import Settings
from ..paths import cache_dir, project_root


def disk_health(settings: Settings, root: Path | None = None) -> dict:
    target = root or project_root()
    free_gb = shutil.disk_usage(target).free / (1024 ** 3)
    minimum = float(settings.get("storage.min_free_gb", 10))
    return {
        "ok": free_gb >= minimum,
        "free_gb": round(free_gb, 2),
        "min_free_gb": minimum,
    }


def retention_audit(
    settings: Settings, root: Path | None = None,
    now: float | None = None,
) -> dict:
    """Count old temporary files without unlinking any on-air assets."""
    base = root or cache_dir()
    current = time.time() if now is None else now
    frame_age = float(settings.get("storage.frame_retention_hours", 12)) * 3600
    intermediate_age = float(
        settings.get("storage.intermediate_retention_days", 3)) * 86400
    results = {}
    for folder, age in (("frames", frame_age), ("audio", intermediate_age),
                        ("shots", intermediate_age)):
        count = size = 0
        directory = base / folder
        if directory.is_dir():
            for path in directory.rglob("*"):
                try:
                    if path.is_file() and not path.is_symlink():
                        stat = path.stat()
                        if current - stat.st_mtime > age:
                            count += 1
                            size += stat.st_size
                except OSError:
                    continue  # A render may replace a temporary file mid-scan.
        results[folder] = {"expired_files": count, "expired_bytes": size}
    results["action"] = "report_only"
    return results
