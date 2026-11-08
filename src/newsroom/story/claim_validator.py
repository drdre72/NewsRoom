"""Claim validator (§12, §47) — deterministic enforcement of the fact contract.

Rules:
  1. Every segment must cite >=1 Fact ID that exists in the ledger context.
  2. High-risk claims (casualties, cause, attribution...) may only appear if
     a cited fact field literally covers them — otherwise reject.
  3. Numbers in a sentence must be traceable to a cited fact's display value
     (magnitude 6.4, depth 17.0 km, coords) or to a benign whitelist
     (clock times, list ordinals). This catches magnitude hallucination.

A failed script is rejected wholesale — the pipeline falls back to the
template generator rather than airing a partially-true script.
"""
from __future__ import annotations

import re

from ..config import Settings
from ..models.fact import Fact
from ..models.story import Script
from .context_builder import FactContext
from .script_generator import (
    _BACKGROUND_BY_TYPE, _BACKGROUND_GENERIC, _DEVELOPING_CLOSE,
    _MONITORING_COPY,
)

# Words that trigger high-risk scrutiny (§15). If present and no cited fact
# carries them, the segment is rejected.
_HIGH_RISK = {
    "casualt": r"\bcasualt", "fatalit": r"\bfatalit", r"deaths?\b": r"\bdeaths?\b",
    "killed": r"\bkilled\b", "injur": r"\binjur", "terror": r"\bterror",
    "attack": r"\battack", "explosion cause": None,
    "damage": r"\bdamag", "destroyed": r"\bdestroy",
}

# Clock times extract as single tokens ("17:30"), not two integers.
_NUM_RE = re.compile(r"\d{1,2}:\d{2}|-?\d+(?:\.\d+)?")
_BENIGN_NUMBERS = re.compile(r"^\d{1,2}:\d{2}$")


def _extract_numbers(text: str) -> list[str]:
    return [m.group(0) for m in _NUM_RE.finditer(text)]


def _is_approved_uncited_copy(script: Script, ctx: FactContext, text: str) -> bool:
    if script.generator == "longform:template":
        return text in (
            _BACKGROUND_BY_TYPE.get(ctx.event_type, _BACKGROUND_GENERIC),
            _DEVELOPING_CLOSE, _MONITORING_COPY,
        )
    return script.generator == "template" and text == _DEVELOPING_CLOSE


def _num_key(token: str) -> float | None:
    try:
        return float(token)
    except ValueError:
        return None


def _fact_number_keys(facts: list[Fact]) -> set[float]:
    """Canonical numeric values present in the ledger. Compared as floats so
    'November 7' traces to ISO '07' and '5.5' to '5.50'."""
    out: set[float] = set()
    for f in facts:
        for n in _extract_numbers(str(f.value_display)):
            if (k := _num_key(n)) is not None:
                out.add(k)
        for n in _extract_numbers(str(f.value)):
            if (k := _num_key(n)) is not None:
                out.add(k)
    return out


def _high_risk_hit(text: str) -> str | None:
    low = text.lower()
    for label, pattern in _HIGH_RISK.items():
        if pattern and re.search(pattern, low):
            return label
    return None


def validate_script(
    script: Script, ctx: FactContext, settings: Settings
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    known = ctx.fact_by_id()
    risk_fields = set(settings.get("high_risk_fields", []) or [])

    if not script.segments:
        errors.append("script has no segments")
    if not script.headline:
        errors.append("script has no headline")

    for i, seg in enumerate(script.segments):
        cited = [fid for fid in seg.fact_ids if fid in known]
        bad = [fid for fid in seg.fact_ids if fid not in known]
        if bad:
            errors.append(f"segment {i}: cites unknown fact ids {bad}")
        if not cited and not _is_approved_uncited_copy(script, ctx, seg.text):
            errors.append(f"segment {i}: no valid fact citations")

        cited_fields = {known[fid].field for fid in cited}
        hit = _high_risk_hit(seg.text)
        if hit and not (cited_fields & risk_fields):
            # Verbatim-sourced claims are supported too: the hit term must
            # appear in a cited fact's value ("Most flood deaths occur in
            # vehicles" quotes the alert's own instruction fact). Numbers
            # remain independently guarded by the traceable-number rule.
            cited_text = " ".join(
                f"{known[fid].value} {known[fid].value_display}".lower()
                for fid in cited)
            pat = _HIGH_RISK.get(hit)
            if not (pat and re.search(pat, cited_text)):
                errors.append(
                    f"segment {i}: high-risk claim '{hit}' unsupported by cited facts")

        fact_number_keys = _fact_number_keys([known[fid] for fid in cited])
        for num in _extract_numbers(seg.text):
            if _BENIGN_NUMBERS.match(num):
                continue
            key = _num_key(num)
            if key is not None and key in fact_number_keys:
                continue
            # Numbers inside cited fact values only. e.g. "6.4" must appear in
            # a fact; a 4-digit year in an ISO timestamp fact is also fine.
            errors.append(f"segment {i}: untraceable number '{num}'")

    return (not errors), errors
