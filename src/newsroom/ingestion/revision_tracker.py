"""Revision tracking (§8, §17).

A material change = any tracked canonical field differing between the new
observation and the current snapshot. Numeric comparisons use a small
tolerance so feed float-jitter doesn't mint empty revisions.
"""
from __future__ import annotations

from typing import Any

_EPS = 1e-9


def _different(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) > _EPS
    return a != b


def diff_snapshots(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """Return sorted list of field names that materially changed."""
    if old is None:
        return sorted(k for k, v in new.items() if v is not None)
    changed = []
    for key in set(old) | set(new):
        if _different(old.get(key), new.get(key)):
            changed.append(key)
    return sorted(changed)
