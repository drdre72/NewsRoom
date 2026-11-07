"""Confidence engine (§14) — separate from impact on purpose.

A catastrophic-but-unverified event can score high impact / low confidence;
that combination must NOT auto-publish. Confidence derives from source
authority, the source's own review flag, and cross-source corroboration.
"""
from __future__ import annotations

from ..config import Settings


def score_confidence(
    source: str,
    reviewed: bool,
    corroborating_sources: int,
    settings: Settings,
) -> float:
    cfg = settings.get("confidence", {}) or {}
    base = float(cfg.get("base_by_source", {}).get(
        source, cfg.get("base_by_source", {}).get("default", 0.5)))
    if reviewed:
        base += float(cfg.get("reviewed_bonus", 0.0))
    extra = max(0, corroborating_sources - 1)
    base += min(2, extra) * float(cfg.get("corroboration_bonus", 0.0))
    return round(min(float(cfg.get("max", 1.0)), base), 3)


def production_gate(score: float, settings: Settings) -> str:
    """§14 policy: eligible | corroborate | hold."""
    cfg = settings.get("confidence", {}) or {}
    if score >= float(cfg.get("auto_publish_min", 0.95)):
        return "eligible"
    if score >= float(cfg.get("corroborate_min", 0.70)):
        return "corroborate"
    return "hold"
