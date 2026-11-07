"""Normalizer — NormalizedObservation -> canonical field snapshot.

The canonical snapshot is the deterministic state of the event at a point in
time. Revisions diff these snapshots; the fact ledger is built from them.
"""
from __future__ import annotations

from typing import Any

from ..models.observation import NormalizedObservation

# Fields that carry over from the normalized observation into the canonical
# snapshot. Everything in `fields` plus these top-level attributes.
_CORE_FIELDS = (
    "occurred_at", "latitude", "longitude", "location_label",
    "title", "severity", "status",
)


def canonical_fields(obs: NormalizedObservation) -> dict[str, Any]:
    snap: dict[str, Any] = {k: getattr(obs, k) for k in _CORE_FIELDS}
    for key, value in obs.fields.items():
        if value is not None:
            snap[key] = value
    return snap


# Display renderers for fact values (kept deterministic).
def _iso_to_display(value: Any) -> str:
    """'2026-11-07T14:31:21Z' -> '14:31 UTC, 07 Nov'."""
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.strftime("%H:%M UTC, %d %b")
    except ValueError:
        return str(value)


def display_value(field: str, value: Any) -> str:
    if value is None:
        return "unknown"
    if field in ("occurred_at", "effective", "onset", "expires", "ends"):
        return _iso_to_display(value)
    if field in ("severity", "magnitude"):
        try:
            v = float(value)
            return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:.1f}"
        except (TypeError, ValueError):
            return str(value)
    if field in ("latitude", "longitude"):
        return f"{float(value):.4f}"
    if field in ("depth_km",):
        return f"{float(value):.1f} km"
    if field == "tsunami":
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)
