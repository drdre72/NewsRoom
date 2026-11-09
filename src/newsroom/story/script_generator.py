"""Script generator (§11, §12).

Primary path: Ollama produces structured JSON against a schema, constrained
to cite Fact IDs. Fallback path: a deterministic template that emits equally
fact-cited segments — used when the LLM is unavailable or its output fails
validation (§37: reduced capability, never fabricated facts).
"""
from __future__ import annotations

import re

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
    add(_DEVELOPING_CLOSE)


# -- long-form explainer (§18) --------------------------------------------
# Chapter markers map to YouTube chapters in the render manifest. Each
# chapter is a list of (text, *fact_refs) tuples; the template builder
# stitches them into one Script with chapter boundary metadata.

_BACKGROUND_BY_TYPE = {
    "earthquake": (
        "Earthquakes occur when built-up tectonic stress is suddenly "
        "released along a fault. The magnitude scale is logarithmic: "
        "each whole number represents roughly thirty-two times more "
        "energy than the one before it."),
    "weather_alert": (
        "The National Weather Service issues alerts at four severity "
        "levels: advisory, watch, warning, and emergency. A warning "
        "means hazardous weather is occurring or imminent."),
    "wildfire": (
        "Wildfires are tracked by satellites that detect thermal "
        "anomalies. Acreage is the standard extent metric; containment "
        "percentages indicate how much of the fire perimeter is "
        "under control."),
    "tropical_cyclone": (
        "Tropical cyclones are rated by sustained wind speed on the "
        "Saffir-Simpson scale. The scale ranges from Category One to "
        "Category Five, with higher categories indicating "
        "increasingly destructive wind speeds."),
    "space_weather": (
        "Space weather storms are rated on the NOAA scale for "
        "geomagnetic storms, solar radiation, and radio blackouts. "
        "Each scale runs from level one minor to level five extreme."),
    "conflict_event": (
        "Conflict event data is aggregated from multiple monitoring "
        "sources. Corroboration across independent sources increases "
        "confidence in the reported details."),
    "economic_indicator": (
        "Economic indicators are released on fixed schedules by "
        "government statistical agencies. Markets watch the surprise "
        "component: how much the actual print differs from "
        "consensus expectations."),
    "disaster_declaration": (
        "FEMA declarations authorize federal assistance. A Major "
        "Disaster Declaration unlocks Individual Assistance and "
        "Public Assistance; an Emergency Declaration covers a "
        "narrower scope of federal support."),
    "cyber_advisory": (
        "The Known Exploited Vulnerabilities catalog maintained by "
        "CISA lists flaws that are being actively exploited in the "
        "wild. Federal agencies are bound by a Binding Operational "
        "Directive to remediate by a specified deadline."),
    "news_report": (
        "Press reports are attributed coverage: the outlet states the "
        "claim, and the newsroom relays it with attribution rather than "
        "asserting it as independently verified fact."),
    "crime_report": (
        "Crime Desk coverage is aggregated from press reports and "
        "official statements. Items are attributed to their sources; "
        "federal confirmations outrank aggregation."),
}
_BACKGROUND_GENERIC = (
    "This event is being monitored by the autonomous newsroom. "
    "Information is sourced from authoritative feeds and verified "
    "against the fact ledger.")
_DEVELOPING_CLOSE = (
    "This is a developing story. Details will be updated as official "
    "sources revise their reports.")
_MONITORING_COPY = (
    "We will continue to monitor authoritative sources for "
    "updates. Revisions will be reported as they are confirmed.")


def generate_longform_script(
    ctx: FactContext,
    plan: StoryPlan,
    settings: Settings,
    event,
    llm: OllamaClient | None = None,
) -> tuple[Script, list[dict]]:
    """Long-form explainer script + chapter markers (§18).

    Returns (Script, chapters) where chapters is a list of
    {"title": str, "start_s": float} for YouTube chapter export.
    The script is a deeper, multi-chapter treatment of the same event:
    lede → what we know (per-type core) → context → revisions →
    corroboration → impact → background → what to watch → close.
    """
    # Build the per-type core segments first (reuse existing templates).
    core = _script_from_template(ctx, plan, settings)

    # Now wrap the core in long-form chapters.
    f, segs, add = _template_parts(ctx, settings)
    chapters: list[dict] = []
    chapter_titles: list[str] = []

    def _mark_chapter(title: str) -> None:
        chapters.append({"title": title, "start_s": 0.0,
                         "first_segment": len(segs)})
        chapter_titles.append(title)

    # -- Chapter 1: Lede
    _mark_chapter("Overview")
    headline_f = f.get("title")
    if headline_f:
        add(headline_f.value_display + ".", "title")
    else:
        add(core.headline + ".")
    alert = f.get("alert_level")
    if alert:
        add(f"The alert level is {alert.value_display}.", "alert_level")

    # -- Chapter 2: What We Know (the per-type core)
    _mark_chapter("What We Know")
    for seg in core.segments:
        if "developing story" in seg.text.lower():
            continue                    # the close is handled separately
        segs.append(seg)
    chapters[-1]["segments"] = len(core.segments)

    # -- Chapter 3: Where (geographic context)
    if "where" in plan.sections:
        _mark_chapter("Location")
        place = f.get("location_label")
        if place:
            add(f"This event is located in {place.value_display}.",
                "location_label")
        lat = f.get("latitude")
        lon = f.get("longitude")
        if lat and lon:
            add(f"The coordinates are {lat.value_display} degrees "
                f"latitude, {lon.value_display} degrees longitude.",
                "latitude", "longitude")

    # -- Chapter 4: When (timeline)
    if "when" in plan.sections:
        _mark_chapter("Timeline")
        when = f.get("occurred_at")
        if when:
            add(f"The event was reported at {when.value_display} UTC.",
                "occurred_at")
        updated = f.get("last_updated")
        if updated and updated.value != (when.value if when else None):
            add(f"The most recent update was at {updated.value_display}.",
                "last_updated")

    # -- Chapter 5: Revisions (how the story evolved)
    if "revisions" in plan.sections and ctx.revisions:
        _mark_chapter("How the Story Evolved")
        _revision_segments(ctx, add)

    # -- Chapter 6: Corroboration (source confirmation)
    if "corroboration" in plan.sections:
        sources = [s for s in (event.source_ids or [])
                   if s and s != (f.get("title", "").value
                                   if f.get("title") else None)]
        src_url = f.get("source_url")
        if len(sources) > 1 and src_url:
            _mark_chapter("Source Records")
            add(f"The latest source record is available at "
                f"{src_url.value_display}.", "source_url")
            cat_title = f.get("category_title")
            if cat_title:
                add(f"The event is categorized as "
                    f"{cat_title.value_display}.", "category_title")

    # -- Chapter 7: Impact
    _mark_chapter("Impact Assessment")
    sev = f.get("severity")
    cat = f.get("category_title") or f.get("category")
    if sev:
        unit = f.get("magnitude_unit")
        suffix = f" {unit.value_display}" if unit else ""
        add(f"The measured severity is {sev.value_display}{suffix}.",
            "severity", *(["magnitude_unit"] if unit else []))
    if cat:
        add(f"Event class: {cat.value_display}.", cat.field)
    alert = f.get("alert_level")
    if alert:
        add(f"The alert level is {alert.value_display}.", "alert_level")
    pop = f.get("population")
    if pop:
        add(f"Affected population: {pop.value_display}.", "population")

    # -- Chapter 8: Background (type context)
    if "background" in plan.sections:
        _mark_chapter("Background")
        bg = _BACKGROUND_BY_TYPE.get(ctx.event_type, _BACKGROUND_GENERIC)
        add(bg)

    # -- Chapter 9: Guidance / what to watch
    if "guidance" in plan.sections:
        _mark_chapter("Official Guidance")
        instr = f.get("instruction")
        if instr:
            add(f"Official instruction: {instr.value_display}", "instruction")
        due = f.get("due_date")
        if due:
            add(f"The remediation deadline is {due.value_display}.",
                "due_date")
        expires = f.get("expires") or f.get("ends")
        if expires:
            add(f"The alert remains in effect until "
                f"{expires.value_display}.", expires.field)

    # -- Chapter 10: What to Watch
    if "what_to_watch" in plan.sections:
        _mark_chapter("What to Watch")
        add(_MONITORING_COPY)

    # -- Final chapter: Close
    _mark_chapter("Closing")
    _developing_close(add)

    script = Script(
        headline=core.headline, segments=segs, generator="longform:template",
        event_id=ctx.event_id, revision=ctx.revision,
        extra={"chapters": chapter_titles},
    )
    return script, chapters


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
    if ctx.event_type == "market_quote":
        return _market_template(ctx, settings)
    if ctx.event_type == "disaster_declaration":
        return _fema_template(ctx, settings)
    if ctx.event_type == "cyber_advisory":
        return _cisa_template(ctx, settings)
    if ctx.event_type in ("news_report", "crime_report"):
        return _news_template(ctx, settings)
    return _eonet_template(ctx, settings)


def _news_template(ctx: FactContext, settings: Settings) -> Script:
    """Press report — attributed coverage. The outlet claims it; we relay
    that claim with attribution, never assert it as verified fact."""
    f, segs, add = _template_parts(ctx, settings)
    outlet = f.get("outlet")
    head = f.get("headline")
    if outlet and head:
        add(f"{outlet.value_display} reports: {head.value_display}",
            "outlet", "headline")
    elif head:
        add(f"Press coverage reports: {head.value_display}", "headline")
    summary = f.get("summary")
    if summary and head:
        text = str(summary.value_display).strip()
        # RSS excerpts may be clipped at ingestion. Never air an incomplete
        # sentence or fill the missing context with invented explanation.
        sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
        headline_words = set(re.findall(r"\w+", str(head.value_display).lower()))
        detail_words = set(re.findall(r"\w+", sentence.lower()))
        if (len(sentence) <= 180 and sentence.endswith((".", "!", "?"))
                and len(detail_words) >= 6
                and len(detail_words - headline_words) >= 4):
            name = str(outlet.value_display) if outlet else "The source"
            add(f"{name} adds: {sentence}", "summary",
                *(["outlet"] if outlet else []))
    name = outlet.value_display if outlet else "Press"
    return Script(headline=f"{name} — {head.value_display}" if head else
                  f"{name} report",
                  segments=segs, generator="template",
                  event_id=ctx.event_id, revision=ctx.revision)


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
        if ctx.event_type == "tropical_cyclone":
            add(f"The source reports wind speed of "
                f"{sev.value_display}{suffix}.",
                "severity", *(["magnitude_unit"] if unit else []))
        else:
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
    val = f.get("rate_value") or f.get("severity")
    units = f.get("units")
    period = f.get("period_name") or f.get("value_date")
    if label and val:
        refs = ["series_label", val.field]
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
    prev = f.get("previous_rate") or f.get("previous_value")
    if dpct and prev:
        refs = ["delta_pct", prev.field]
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


def _market_template(ctx: FactContext, settings: Settings) -> Script:
    """Quote only observed prices and ledger-derived moves; no forecast."""
    f, segs, add = _template_parts(ctx, settings)
    label, price = f.get("series_label"), f.get("severity")
    pair, units = f.get("pair"), f.get("units")
    if label and price and pair and units:
        # Market quotes are two-decimal snapshots in the fact ledger.
        add(f"Coinbase reports {label.value_display} at "
            f"{price.value:.2f} {units.value_display} per unit, "
            f"for {pair.value_display}.",
            "series_label", "severity", "units", "pair")
    prev, pct = f.get("previous_value"), f.get("delta_pct")
    if prev and pct and units:
        add(f"The prior observed quote was {prev.value:.2f} "
            f"{units.value_display}, a change of "
            f"{pct.value_display} percent since that observation.",
            "previous_value", "units", "delta_pct")
    _developing_close(add)
    return Script(headline=(f"{label.value_display} spot price" if label
                            else "Market quote"), segments=segs,
                  generator="template", event_id=ctx.event_id,
                  revision=ctx.revision)


def _fema_template(ctx: FactContext, settings: Settings) -> Script:
    """FEMA declarations — the declaration class, the state, the covered
    counties, and the incident behind it."""
    f, segs, add = _template_parts(ctx, settings)

    label = f.get("declaration_label")
    state = f.get("state")
    incident = f.get("incident_type")
    if label and state:
        add(f"FEMA has issued a {label.value_display.lower()} for "
            f"{state.value_display}.", "declaration_label", "state")
    elif label:
        add(f"FEMA has issued a {label.value_display.lower()}.",
            "declaration_label")

    if incident:
        add(f"The declaration follows {incident.value_display.lower()} "
            f"conditions in the affected area.", "incident_type")

    areas = f.get("areas")
    count = f.get("area_count")
    if areas:
        add(f"Designated areas: {areas.value_display}.", "areas")
    elif count:
        add(f"The declaration covers {count.value_display} designated "
            f"areas.", "area_count")

    when = f.get("occurred_at")
    if when:
        add(f"The incident began {when.value_display}.", "occurred_at")

    _revision_segments(ctx, add)
    _developing_close(add)

    headline = (f"{state.value_display} {incident.value_display.lower()} "
                "declaration" if state and incident else
                (label.value_display if label else "FEMA declaration"))
    return Script(headline=headline, segments=segs, generator="template",
                  event_id=ctx.event_id, revision=ctx.revision)


def _cisa_template(ctx: FactContext, settings: Settings) -> Script:
    """CISA KEV additions — the CVE, what's affected, the federal deadline.
    Careful phrasing: 'exploited in the wild', never unattributed claims."""
    f, segs, add = _template_parts(ctx, settings)

    cve = f.get("cve_id")
    name = f.get("vuln_name")
    if cve and name:
        add(f"CISA has added {cve.value_display} to the Known Exploited "
            f"Vulnerabilities catalog: {name.value_display}.",
            "cve_id", "vuln_name")

    vendor = f.get("vendor")
    product = f.get("product")
    if vendor and product:
        add(f"The affected product is {vendor.value_display} "
            f"{product.value_display}.", "vendor", "product")

    due = f.get("due_date")
    if due:
        add(f"Federal agencies are directed to remediate by "
            f"{due.value_display}.", "due_date")

    ransom = f.get("ransomware_use")
    if ransom and ransom.value == "Known":
        add("CISA reports this vulnerability is used in known ransomware "
            "campaigns.", "ransomware_use")

    _revision_segments(ctx, add)
    _developing_close(add)

    headline = (f"{cve.value_display} actively exploited" if cve
                else "Cybersecurity advisory")
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
