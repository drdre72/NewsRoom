"""Impact engine (§13) — deterministic importance scoring.

Features normalize to 0..1, weights come from thresholds.yaml, and the sum
maps to a level. No model, no judgment: same input always scores the same.
"""
from __future__ import annotations

from typing import Any

from ..config import Settings
from ..models.event import ImpactLevel


def _norm(value: float | None, lo: float, hi: float) -> float:
    if value is None or hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (float(value) - lo) / (hi - lo)))


def _earthquake_score(snapshot: dict[str, Any], cfg: dict) -> float:
    total = 0.0
    mag_cfg = cfg.get("magnitude", {})
    total += mag_cfg.get("weight", 0) * _norm(
        snapshot.get("magnitude") or snapshot.get("severity"),
        mag_cfg.get("min", 2.5), mag_cfg.get("max", 9.5))
    sig_cfg = cfg.get("significance", {})
    total += sig_cfg.get("weight", 0) * _norm(
        snapshot.get("sig"), sig_cfg.get("min", 0), sig_cfg.get("max", 1000))
    ts_cfg = cfg.get("tsunami_flag", {})
    total += ts_cfg.get("weight", 0) * (1.0 if snapshot.get("tsunami") else 0.0)
    al_cfg = cfg.get("alert_level", {})
    alert = (snapshot.get("alert") or "").lower()
    total += al_cfg.get("weight", 0) * al_cfg.get("values", {}).get(alert, 0.0)
    sh_cfg = cfg.get("shallow_depth", {})
    depth = snapshot.get("depth_km")
    if depth is not None:
        depth_score = 1.0 if depth <= 35 else (0.5 if depth <= 70 else 0.0)
        total += sh_cfg.get("weight", 0) * depth_score
    return min(1.0, total)


def score_impact(
    event_type: str, snapshot: dict[str, Any], settings: Settings
) -> tuple[float, str]:
    """Return (score 0..1, ImpactLevel value)."""
    engine_cfg = settings.get("impact", {}) or {}
    type_cfg = engine_cfg.get(event_type, {})
    score = _earthquake_score(snapshot, type_cfg) if event_type == "earthquake" else 0.0

    level = ImpactLevel.BACKGROUND.value
    for name, cutoff in (engine_cfg.get("levels", {}) or {}).items():
        if score >= float(cutoff):
            level = name
            break
    return round(score, 3), level
