"""Script generator (§11, §12).

Primary path: Ollama produces structured JSON against a schema, constrained
to cite Fact IDs. Fallback path: a deterministic template that emits equally
fact-cited segments — used when the LLM is unavailable or its output fails
validation (§37: reduced capability, never fabricated facts).
"""
from __future__ import annotations

from ..audio.pronunciation import to_speech_text
from ..llm.ollama import OllamaClient
from ..llm.schemas import SCRIPT_SCHEMA
from ..config import Settings
from ..logging import get_logger, log_event
from ..models.story import Script, ScriptSegment, StoryPlan
from .context_builder import FactContext

log = get_logger("script")

SYSTEM_PROMPT = """You are a factual breaking-news bulletin writer.

You may ONLY use factual claims contained in FACTS below. Every factual
sentence MUST include the Fact IDs that support it.

Do not invent or speculate about: casualties, injuries, damage, causes,
responsibility, intent, predictions, or future outcomes. If information is
unknown, omit it entirely.

Be terse, neutral, and precise. This is broadcast copy — short sentences,
no adjectives of drama, no rhetorical questions.

Respond with JSON only: {"headline": "...", "segments": [{"text": "...",
"fact_ids": ["f_...", ...]}, ...]}"""


def _llm_prompt(ctx: FactContext, plan: StoryPlan) -> str:
    sections = ", ".join(plan.sections)
    return (
        f"Write a {plan.duration_target_s}-second breaking news bulletin script "
        f"for this event. Cover these sections in order: {sections}.\n\n"
        f"FACTS:\n{ctx.prompt_block}\n\n"
        "Every factual sentence must cite supporting Fact IDs in fact_ids. "
        "If a section has no supporting facts, skip that section."
    )


def _apply_speech(text: str, settings: Settings) -> str:
    overrides = settings.get("pronunciation.overrides", {}) or {}
    return to_speech_text(text, overrides)


def generate_script(
    ctx: FactContext,
    plan: StoryPlan,
    settings: Settings,
    llm: OllamaClient | None = None,
) -> Script:
    """LLM first; deterministic template as the sanctioned fallback."""
    llm = llm or OllamaClient(settings)
    ok, detail = llm.available()
    if ok:
        result = llm.generate_json(_llm_prompt(ctx, plan), schema=SCRIPT_SCHEMA,
                                   system=SYSTEM_PROMPT)
        if result.ok and result.json:
            script = _script_from_llm(result.json, ctx, plan, settings,
                                      generator=f"ollama:{result.model}")
            script.extra["llm_latency_ms"] = result.latency_ms
            log_event(log, 20, "script generated via llm",
                      model=result.model, latency_ms=result.latency_ms,
                      event_id=ctx.event_id)
            return script
        log_event(log, 40, "llm output unusable; falling back to template",
                  error=result.error, event_id=ctx.event_id)
    else:
        log_event(log, 30, "llm unavailable; using template",
                  detail=detail, event_id=ctx.event_id)
    return _script_from_template(ctx, plan, settings)


def _script_from_llm(
    data: dict, ctx: FactContext, plan: StoryPlan, settings: Settings,
    generator: str,
) -> Script:
    segments = []
    for seg in data.get("segments", []):
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        segments.append(ScriptSegment(
            text=text,
            speech_text=_apply_speech(text, settings),
            fact_ids=[str(fid) for fid in seg.get("fact_ids", [])],
        ))
    return Script(
        headline=str(data.get("headline", "")).strip(),
        segments=segments, generator=generator,
        event_id=ctx.event_id, revision=ctx.revision,
    )


def _script_from_template(
    ctx: FactContext, plan: StoryPlan, settings: Settings
) -> Script:
    """Deterministic fact-cited bulletin — always valid, always honest."""
    f = {fact.field: fact for fact in ctx.current}
    known_ids = {fact.fact_id for fact in ctx.all_facts or ctx.current}
    segs: list[ScriptSegment] = []

    def add(text: str, *refs: str) -> None:
        """Each ref is a field name or a concrete fact_id (e.g. a superseded
        fact referenced by a revision sentence)."""
        ids = []
        for ref in refs:
            if ref in f:
                ids.append(f[ref].fact_id)
            elif ref in known_ids:
                ids.append(ref)
        segs.append(ScriptSegment(
            text=text, speech_text=_apply_speech(text, settings), fact_ids=ids,
        ))

    mag = f.get("magnitude") or f.get("severity")
    place = f.get("location_label")
    if mag and place:
        add(f"A magnitude {mag.value_display} earthquake struck {place.value_display}.",
            mag.field, "location_label")
    elif mag:
        add(f"A magnitude {mag.value_display} earthquake has been reported.",
            mag.field)

    when = f.get("occurred_at")
    depth = f.get("depth_km")
    if when and depth:
        add(f"It occurred at {when.value_display} at a depth of {depth.value_display}.",
            "occurred_at", "depth_km")
    elif when:
        add(f"It occurred at {when.value_display}.", "occurred_at")

    tsu = f.get("tsunami")
    if tsu is not None:
        add("A tsunami threat has been flagged by the source feed."
            if tsu.value else "No tsunami threat was indicated by the source feed.",
            "tsunami")

    alert = f.get("alert")
    if alert:
        add(f"The PAGER alert level is {alert.value_display}.", "alert")

    status = f.get("status")
    if status:
        add(f"The source event status is {status.value_display}.", "status")

    for rev in ctx.revisions:
        if rev["field"] in ("magnitude", "severity"):
            add(f"The magnitude was initially reported as {rev['initial']} "
                f"and has since been revised to {rev['current']}.",
                rev["initial_fact_id"], rev["current_fact_id"])

    add("This is a developing story. Details will be updated as official "
        "sources revise their reports.")

    headline = (
        f"M{mag.value_display} earthquake — {place.value_display}"
        if mag and place else
        (f"Magnitude {mag.value_display} earthquake reported" if mag
         else "Breaking earthquake bulletin")
    )
    return Script(headline=headline, segments=segs, generator="template",
                  event_id=ctx.event_id, revision=ctx.revision)
