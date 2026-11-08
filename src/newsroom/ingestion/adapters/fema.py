"""FEMA adapter — OpenFEMA disaster declarations (fema.gov/openfema).

Each femaDeclarationString (DR-####-ST / EM-####-ST / FM-####-ST) is a stable
`disaster_declaration` event. The API returns one row per designated county;
fetch groups rows so one payload = one declaration with an `areas` list —
new counties land as revisions, not churn. severity_norm comes from the
declaration class: DR (major disaster) > EM (emergency) > FM (fire mgmt).
Public API — no key required.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Iterable

from .base import SourceAdapter, FetchResult
from ...models.observation import NormalizedObservation
from ...security import fetch_bytes

_BASE = ("https://www.fema.gov/api/open/v2/"
         "DisasterDeclarationsSummaries")

_TYPE_NORM = {"DR": 1.0, "EM": 0.7, "FM": 0.4}
_TYPE_LABEL = {"DR": "Major Disaster Declaration",
               "EM": "Emergency Declaration",
               "FM": "Fire Management Assistance Declaration"}


class FemaAdapter(SourceAdapter):
    kind = "fema"
    event_type = "disaster_declaration"

    def fetch(self, timeout: int = 20, user_agent: str = "newsroom/0.1") -> FetchResult:
        from ...storage.repositories import utcnow
        top = int(self.config.get("top", 60))
        q = urllib.parse.urlencode(
            {"$orderby": "declarationDate desc", "$top": top})
        url = f"{_BASE}?{q}"
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        doc = json.loads(fetch_bytes(req, timeout=timeout).decode("utf-8"))
        rows = doc.get("DisasterDeclarationsSummaries") or []
        # One payload per declaration — rows are per designated county.
        decls: dict[str, dict] = {}
        for r in rows:
            key = r.get("femaDeclarationString")
            if not key:
                continue
            d = decls.setdefault(key, {"row": r, "areas": []})
            area = r.get("designatedArea")
            if area and area not in d["areas"]:
                d["areas"].append(area)
        payloads = [
            json.dumps({"declaration": k, "areas": v["areas"], **v["row"]})
            for k, v in decls.items()
        ]
        return FetchResult(feed_url=url, retrieved_at=utcnow(),
                           payloads=payloads)

    def split_items(self, body: str) -> Iterable[str]:
        yield body                       # one payload per declaration already

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        doc = json.loads(raw_payload)
        decl = doc.get("declaration")
        if not decl:
            return None
        dtype = doc.get("declarationType") or "DR"
        state = doc.get("state") or ""
        incident = doc.get("incidentType") or "Disaster"
        areas = doc.get("areas") or []
        title = (doc.get("declarationTitle") or f"{incident} in {state}"
                 ).title()
        label = _TYPE_LABEL.get(dtype, f"{dtype} Declaration")
        fields = {
            "declaration_id": decl, "declaration_type": dtype,
            "declaration_label": label, "state": state,
            "incident_type": incident, "areas": ", ".join(areas),
            "area_count": len(areas),
            "ih_program": bool(doc.get("ihProgramDeclared")),
            "pa_program": bool(doc.get("paProgramDeclared")),
            "category": incident, "category_title": label,
            "severity_norm": _TYPE_NORM.get(dtype, 0.3),
            "magnitude_unit": dtype,
            "source_url": ("https://www.fema.gov/disaster/"
                           + str(doc.get("disasterNumber", ""))),
        }
        return NormalizedObservation(
            source="fema", source_event_id=decl,
            event_type="disaster_declaration",
            occurred_at=doc.get("incidentBeginDate") or "",
            source_updated_at=doc.get("declarationDate"),
            latitude=None, longitude=None,
            location_label=f"{state}, United States" if state else "United States",
            title=f"FEMA {label}: {title} — {state}",
            severity=_TYPE_NORM.get(dtype, 0.3), status="declared",
            reviewed=True, fields=fields, cross_references=[])
