"""Story planning and script models (§11, §12, §18).

The LLM receives explicit facts and must cite them. ScriptSegment.fact_ids
is the contract the claim validator enforces.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class StoryPlan:
    story_type: str                      # breaking | update | explainer
    duration_target_s: int
    sections: list[str]
    event_id: str
    revision: int


@dataclass
class ScriptSegment:
    text: str                            # display text
    speech_text: str                     # pronunciation-adjusted (§24)
    fact_ids: list[str]
    duration_s: float = 0.0              # filled by alignment


@dataclass
class Script:
    headline: str
    segments: list[ScriptSegment]
    generator: str                       # ollama:<model> | template
    event_id: str
    revision: int
    validated: bool = False
    validation_errors: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Script":
        """Rebuild a stored script — recipes replay these verbatim (§52)."""
        return cls(
            headline=d["headline"],
            segments=[ScriptSegment(
                text=s["text"], speech_text=s.get("speech_text", s["text"]),
                fact_ids=list(s.get("fact_ids", [])),
                duration_s=float(s.get("duration_s", 0.0)),
            ) for s in d["segments"]],
            generator=d.get("generator", "recipe"),
            event_id=d["event_id"], revision=int(d["revision"]),
            validated=bool(d.get("validated", False)),
            validation_errors=list(d.get("validation_errors", [])),
            extra=dict(d.get("extra", {})),
        )
