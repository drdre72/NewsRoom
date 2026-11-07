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
