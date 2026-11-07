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
no adjectives of drama, no rhetorical questions. When stating a number,
use the exact value shown in the cited fact (e.g. "6.4", "22:00 UTC, 07 Nov").

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


# Friendly field names for "initially reported X, since revised to Y" (§10).
_FIELD_LABEL = {
    "severity": "magnitude", "severity_text": "severity",
    "alert_event": "alert type", "depth_km": "depth",
    "certainty": "alert certainty", "urgency": "alert urgency",
    "expires": "alert expiration", "occurred_at": "event time",
}

# Only these supersessions are spoken in a bulletin. Every change stays in
# the ledger and the LLM's FACTS block; the template just doesn't narrate
# cosmetic churn (headline wording, message bookkeeping).
_REVISION_WORTHY = {
    "magnitude", "severity", "severity_text", "certainty", "urgency",
    "alert_event", "expires", "depth_km", "tsunami", "alert",
    "location_label", "occurred_at", "motion",
}


def _template_parts(ctx: FactContext, settings: Settings):
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

    return f, segs, add


def _revision_segments(ctx: FactContext, add) -> None:
    """'was X, now Y' sentences for any field with superseded history (§10)."""
    # `severity` is the generic scalar — when a family-specific field carries
    # the same change (magnitude / severity_text), don't say it twice.
    specific = {"magnitude", "severity_text"}
    skip = {"severity"} if any(
        r["field"] in specific for r in ctx.revisions) else set()
    for rev in ctx.revisions:
        if rev["field"] in skip or rev["field"] not in _REVISION_WORTHY:
            continue
        label = _FIELD_LABEL.get(rev["field"], rev["field"].replace("_", " "))
        add(f"The {label} was initially reported as {rev['initial']} "
            f"and has since been revised to {rev['current']}.",
            rev["initial_fact_id"], rev["current_fact_id"])


def _developing_close(add) -> None:
    add("This is a developing story. Details will be updated as official "
        "sources revise their reports.")


def _script_from_template(
    ctx: FactContext, plan: StoryPlan, settings: Settings
) -> Script:
    """Deterministic fact-cited bulletin — always valid, always honest."""
    if ctx.event_type == "weather_alert":
        return _weather_template(ctx, settings)
    if ctx.event_type == "earthquake":
        return _earthquake_template(ctx, settings)
    if ctx.event_type == "economic_indicator":
        return _econ_template(ctx, settings)
    return _eonet_template(ctx, settings)


def _weather_template(ctx: FactContext, settings: Settings) -> Script:
    f, segs, add = _template_parts(ctx, settings)

    alert = f.get("alert_event")
    area = f.get("location_label")
    sender = f.get("sender_name")
    if alert and sender and area:
        add(f"{sender.value_display} has issued a {alert.value_display} "
            f"for {area.value_display}.", "sender_name", "alert_event",
            "location_label")
    elif alert:
        add(f"A {alert.value_display} has been issued.", "alert_event")

    when = f.get("occurred_at")
    expiry = f.get("expires") or f.get("ends")
    if when and expiry:
        add(f"It is in effect from {when.value_display} until "
            f"{expiry.value_display}.", "occurred_at",
            "expires" if f.get("expires") else "ends")
    elif expiry:
        add(f"It remains in effect until {expiry.value_display}.",
            "expires" if f.get("expires") else "ends")

    sev = f.get("severity_text")
    cert = f.get("certainty")
    urg = f.get("urgency")
    if sev and cert and urg:
        add(f"The alert is rated {sev.value_display} severity, with "
            f"{cert.value_display} certainty and {urg.value_display} urgency.",
            "severity_text", "certainty", "urgency")
    elif sev:
        add(f"The alert is rated {sev.value_display} severity.",
            "severity_text")

    motion = f.get("motion")
    if motion:
        add(f"Reported motion: {motion.value_display.capitalize()}.", "motion")

    instr = f.get("instruction")
    if instr:
        add(f"Official instruction: {instr.value_display}", "instruction")

    _revision_segments(ctx, add)
    _developing_close(add)

    headline = (
        f"{alert.value_display} — {area.value_display}" if alert and area else
        (f"{alert.value_display} issued" if alert else "Weather alert bulletin")
    )
    return Script(headline=headline, segments=segs, generator="template",
                  event_id=ctx.event_id, revision=ctx.revision)


def _eonet_template(ctx: FactContext, settings: Settings) -> Script:
    """EONET natural events (wildfire, volcano, flood, ...) — severity carries
    a unit (acres, kts, VEI) via `magnitude_unit`, never 'magnitude'."""
    f, segs, add = _template_parts(ctx, settings)

    cat = f.get("category_title") or f.get("category")
    title_f = f.get("title")
    place = f.get("location_label")
    kind = cat.value_display if cat else "event"
    if title_f:
        add(f"{title_f.value_display}.", "title")
    elif cat and place:
        add(f"A {kind.lower()} event is being tracked in "
            f"{place.value_display}.", cat.field, "location_label")

    sev = f.get("severity")
    unit = f.get("magnitude_unit")
    fat = f.get("fatalities")
    if fat and fat.value:
        # §15: fatality claims must cite the fatalities fact itself.
        add(f"The source reports {fat.value_display} fatalities.",
            "fatalities")
    elif sev:
        suffix = f" {unit.value_display}" if unit else ""
        add(f"The latest measured extent is {sev.value_display}{suffix}.",
            "severity", *(["magnitude_unit"] if unit else []))

    alert_level = f.get("alert_level")
    if alert_level:
        add(f"The alert level is {alert_level.value_display}.", "alert_level")

    when = f.get("occurred_at")
    if when:
        add(f"The latest position was reported at {when.value_display}.",
            "occurred_at")

    closed = f.get("closed")
    if closed and closed.value:
        ended = f.get("closed_at")
        add(f"The source has marked this event closed"
            + (f" as of {ended.value_display}." if ended else "."),
            "closed", *(["closed_at"] if ended else []))

    _revision_segments(ctx, add)
    _developing_close(add)

    headline = (
        f"{kind} — {place.value_display}" if cat and place else
        (title_f.value_display if title_f else f"{kind} bulletin")
    )
    return Script(headline=headline, segments=segs, generator="template",
                  event_id=ctx.event_id, revision=ctx.revision)


def _econ_template(ctx: FactContext, settings: Settings) -> Script:
    """Economic releases (FRED/BLS/EIA): the print, the period, the move.
    All numbers come from value_display verbatim — the validator's numeric
    traceability rule requires claim figures to match ledger values."""
    f, segs, add = _template_parts(ctx, settings)

    label = f.get("series_label")
    val = f.get("severity")            # the print itself lands in severity
    units = f.get("units")
    period = f.get("period_name") or f.get("value_date")
    if label and val:
        refs = ["series_label", "severity"]
        text = f"{label.value_display} came in at {val.value_display}"
        if units:
            text += f" {units.value_display}"
            refs.append("units")
        if period:
            text += f" for {period.value_display}."
            refs.append(period.field)
        else:
            text += "."
        add(text, *refs)

    delta = f.get("delta")
    dpct = f.get("delta_pct")
    prev = f.get("previous_value")
    if dpct and prev:
        refs = ["delta_pct", "previous_value"]
        text = (f"That compares with {prev.value_display}"
                + (f" {units.value_display}" if units else "")
                + " in the previous reading — a move of "
                  f"{dpct.value_display} percent.")
        if units:
            refs.append("units")
        add(text, *refs)
    elif delta and units:
        add(f"The change from the previous reading was "
            f"{delta.value_display} {units.value_display}.",
            "delta", "units")

    _revision_segments(ctx, add)
    _developing_close(add)

    headline = (f"{label.value_display}" if label else
                "Economic data bulletin")
    return Script(headline=headline, segments=segs, generator="template",
                  event_id=ctx.event_id, revision=ctx.revision)


def _earthquake_template(ctx: FactContext, settings: Settings) -> Script:
    f, segs, add = _template_parts(ctx, settings)

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

    _revision_segments(ctx, add)
    _developing_close(add)

    headline = (
        f"M{mag.value_display} earthquake — {place.value_display}"
        if mag and place else
        (f"Magnitude {mag.value_display} earthquake reported" if mag
         else "Breaking earthquake bulletin")
    )
    return Script(headline=headline, segments=segs, generator="template",
                  event_id=ctx.event_id, revision=ctx.revision)
