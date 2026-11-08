"""Persist poll outcomes separately from evidence freshness.

An unchanged or empty feed can be fetched successfully without inserting an
observation. Health must reflect the fetch, not the age of its last event.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..paths import logs_dir


def _path(path: Path | None) -> Path:
    return path if path is not None else logs_dir() / "source_health.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_poll(
    settings: Settings, report: dict, path: Path | None = None,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Atomically save only status metadata, never payloads or API keys."""
    target = _path(path)
    when = (now or datetime.now(timezone.utc)).isoformat()
    previous = _read(target)
    states = {}
    for name, cfg in (settings.get("sources", {}) or {}).items():
        if not cfg.get("enabled"):
            continue
        prior = previous.get(name, {})
        current = report.get("sources", {}).get(name, {})
        if current.get("skipped") == "interval":
            if not prior and conn is not None:
                row = conn.execute(
                    "SELECT MAX(retrieved_at) FROM observations WHERE source=?",
                    (name,)).fetchone()
                if row and row[0]:
                    prior = {
                        "result": "ok", "last_attempt": row[0],
                        "last_success": row[0],
                    }
            states[name] = prior
            continue
        if not current:
            states[name] = prior
            continue
        state = {
            "last_attempt": when,
            "last_success": prior.get("last_success"),
            "result": "credentials" if current.get("skipped") == "credentials"
                      else "ok" if current.get("ok") else "error",
        }
        if state["result"] == "ok":
            state["last_success"] = when
        if state["result"] == "error":
            state["error"] = str(current.get("error", "fetch failed"))[:200]
        states[name] = state
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".source_health-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(states, stream)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def source_statuses(
    settings: Settings, path: Path | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Report every enabled source, including empty and unchanged feeds."""
    states = _read(_path(path))
    current = now or datetime.now(timezone.utc)
    out = []
    for name, cfg in (settings.get("sources", {}) or {}).items():
        if not cfg.get("enabled"):
            continue
        state = states.get(name, {})
        success = state.get("last_success")
        try:
            age = max(0.0, (current - datetime.fromisoformat(success)).total_seconds()) \
                if success else None
        except (TypeError, ValueError):
            age = None
        interval = float(cfg.get("poll_every_seconds") or
                         settings.get("polling.interval_seconds", 60))
        result = state.get("result")
        status = ("credentials" if result == "credentials" else
                  "error" if result == "error" else
                  "unknown" if age is None else
                  "stale" if age > max(300.0, interval * 3) else "healthy")
        out.append({
            "source": name, "status": status,
            "age_seconds": round(age, 1) if age is not None else None,
            "last_attempt": state.get("last_attempt"),
            "last_success": success,
            "error": state.get("error") if status == "error" else None,
        })
    return out
