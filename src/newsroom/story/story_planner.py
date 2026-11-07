"""Story planner (§18) — structured intent before prose, per event type.

Deterministic: the plan derives from event state, not from the LLM.
"""
from __future__ import annotations

import sqlite3

from ..models.event import CanonicalEvent
from ..models.story import StoryPlan
from ..storage import repositories as repo

_SHORT_TARGET = 45
_LONG_TARGET = 360         # ~6 min — the YouTube explainer leg


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


def plan_longform(
    conn: sqlite3.Connection, event_id: str
) -> StoryPlan | None:
    """Long-form explainer plan — deeper chapters than a short (§18).

    The same fact ledger feeds a longer, multi-chapter treatment: lede,
    what we know, geographic and temporal context, how the story evolved
    across revisions, source corroboration, impact assessment, background
    on the event type, and what to watch next. Each chapter maps to a
    YouTube chapter marker in the render manifest.
    """
    event = repo.get_event(conn, event_id)
    if not event:
        return None
    facts = repo.current_facts(conn, event_id)
    fields = {f.field for f in facts}
    revisions = repo.get_revisions(conn, event_id)
    sources = repo.get_event_source_ids(conn, event_id)

    sections = ["lede", "what_we_know"]
    if event.latitude is not None:
        sections.append("where")
    sections.append("when")
    if len(revisions) > 1:
        sections.append("revisions")
    if len(sources) > 1:
        sections.append("corroboration")
    sections.append("impact")
    # Type-specific background
    sections.append("background")
    # Official guidance / what to watch
    if "instruction" in fields or "due_date" in fields or "expires" in fields:
        sections.append("guidance")
    sections.append("what_to_watch")
    sections.append("close")
    return StoryPlan(
        story_type="explainer",
        duration_target_s=_LONG_TARGET,
        sections=sections,
        event_id=event_id,
        revision=event.current_revision,
    )
