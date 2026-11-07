"""Story planner (§18) — structured intent before prose, per event type.

Deterministic: the plan derives from event state, not from the LLM.
"""
from __future__ import annotations

import sqlite3

from ..models.event import CanonicalEvent
from ..models.story import StoryPlan
from ..storage import repositories as repo

_SHORT_TARGET = 45


def _earthquake_sections(event: CanonicalEvent, facts) -> list[str]:
    sections = ["headline", "magnitude_location", "time_depth"]
    fields = {f.field for f in facts}
    if "tsunami" in fields:
        sections.append("tsunami_status")
    if "alert" in fields or "sig" in fields:
        sections.append("official_assessment")
    sections.append("unknowns")
    return sections


def _weather_alert_sections(event: CanonicalEvent, facts) -> list[str]:
    sections = ["headline", "alert_and_area", "timing"]
    fields = {f.field for f in facts}
    if {"severity_text", "certainty", "urgency"} & fields:
        sections.append("official_assessment")
    if "instruction" in fields:
        sections.append("instructions")
    sections.append("unknowns")
    return sections


def plan_story(
    conn: sqlite3.Connection, event_id: str, story_type: str = "breaking"
) -> StoryPlan | None:
    event = repo.get_event(conn, event_id)
    if not event:
        return None
    facts = repo.current_facts(conn, event_id)
    if event.event_type == "earthquake":
        sections = _earthquake_sections(event, facts)
    elif event.event_type == "weather_alert":
        sections = _weather_alert_sections(event, facts)
    else:
        sections = ["headline", "what", "where", "when", "unknowns"]
    return StoryPlan(
        story_type=story_type,
        duration_target_s=_SHORT_TARGET,
        sections=sections,
        event_id=event_id,
        revision=event.current_revision,
    )
