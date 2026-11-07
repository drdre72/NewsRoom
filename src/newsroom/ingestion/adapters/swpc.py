"""NOAA Space Weather Prediction Center adapter (services.swpc.noaa.gov).

SWPC issues coded alert/warning/summary messages for geomagnetic storms
(G1-G5), solar radiation storms (S1-S5), radio blackouts (R1-R5), and
planetary K-index threshold crossings.

Each message is unique (product_id + serial), but the SAME alert type
repeats throughout a storm — so observations are keyed by message code +
threshold signature, making successive serials revise one canonical event
instead of flooding the rundown (§16).
"""
from __future__ import annotations

import json
import re
from typing import Iterable

from .base import SourceAdapter
from ...models.observation import NormalizedObservation

_KIND = re.compile(r"^(ALERT|WARNING|WATCH|SUMMARY|EXTENDED WARNING):\s*(.+)",
                   re.MULTILINE)
_CODE = re.compile(r"Space Weather Message Code:\s*(\S+)")
_KINDEX = re.compile(r"K-index of\s*(\d+)|Kp\s*=?(\d)", re.IGNORECASE)
_SCALE = re.compile(r"\b([GSR])([1-5])\b")


def _k_or_scale(msg: str) -> tuple[float | None, str | None, str | None]:
    """Return (severity, unit, noaa_scale) parsed from the message body.
    K-index wins for severity; the NOAA scale (G1-G5/R/S) is always
    captured separately for alert-level scoring."""
    s = _SCALE.search(msg)
    scale = f"{s.group(1)}{s.group(2)}" if s else None
    m = _KINDEX.search(msg)
    if m:
        return float(m.group(1) or m.group(2)), "K-index", scale
    if s:
        return float(s.group(2)), f"NOAA {s.group(1)} scale", scale
    return None, None, None


def _headline(msg: str) -> str | None:
    for kind, text in _KIND.findall(msg):
        t = " ".join(text.split())
        if t:
            return f"{kind.capitalize()}: {t}"
    return None


class SwpcAdapter(SourceAdapter):
    kind = "noaa_swpc"
    event_type = "space_weather"

    def split_items(self, body: str) -> Iterable[str]:
        doc = json.loads(body)
        items = doc if isinstance(doc, list) else doc.get("alerts", [doc])
        for item in items:
            yield json.dumps(item, separators=(",", ":"), sort_keys=True)

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        item = json.loads(raw_payload)
        msg = item.get("message") or ""
        product_id = item.get("product_id")
        code = _CODE.search(msg)
        swpc_code = code.group(1) if code else product_id
        if not swpc_code or not msg:
            return None
        sev, unit, scale = _k_or_scale(msg)
        headline = _headline(msg)
        # Same-code alerts for the same threshold revise one event; a new
        # threshold signature (e.g. K=5 -> K=7) is a distinct escalation.
        sev_key = int(sev) if sev is not None else 0
        source_event_id = f"{swpc_code}-k{sev_key}"
        issued = (item.get("issue_datetime") or "").replace(" ", "T")
        if issued and not issued.endswith("Z"):
            issued += "Z"
        fields = {
            "swpc_code": swpc_code,
            "noaa_scale": scale,
            "alert_level": scale,   # generic scorer's alert-level factor
            "magnitude_unit": unit,
            "product_id": product_id,
            "category": "spaceWeather",
            "category_title": "Space Weather",
            "description": " ".join(msg.split())[:2000],
        }
        return NormalizedObservation(
            source="swpc",
            source_event_id=source_event_id,
            event_type="space_weather",
            occurred_at=issued or "",
            source_updated_at=issued or None,
            latitude=None, longitude=None,
            location_label=None,
            title=headline or product_id or swpc_code,
            severity=sev,
            status="active",
            reviewed=False,
            fields=fields,
            cross_references=[],
        )
