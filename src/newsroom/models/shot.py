"""Visual plan models (§19).

Visual intent is data. Renderers execute plans; they make no editorial
decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Shot:
    type: str                            # globe_to_location | impact_radius | metric_card | …
    duration_s: float
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class VisualPlan:
    event_id: str
    revision: int
    width: int
    height: int
    fps: int
    shots: list[Shot] = field(default_factory=list)
    style: dict[str, Any] = field(default_factory=dict)

    def total_duration(self) -> float:
        return sum(s.duration_s for s in self.shots)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VisualPlan":
        return cls(
            event_id=d["event_id"], revision=int(d["revision"]),
            width=int(d["width"]), height=int(d["height"]),
            fps=int(d["fps"]),
            shots=[Shot(type=s["type"], duration_s=float(s["duration_s"]),
                        params=dict(s.get("params", {})))
                   for s in d.get("shots", [])],
            style=dict(d.get("style", {})),
        )
