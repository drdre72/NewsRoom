"""NOAA NHC adapter — National Hurricane Center active storms.

Official NHC CurrentStorms.json — active tropical cyclones with
classification, intensity, position, and advisory numbers. Shares
`tropical_cyclone` with GDACS; reports can fuse if time and geography
align. severity_norm from storm classification and wind speed.
Public API — no key required.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Iterable

from .base import SourceAdapter, FetchResult
from ...models.observation import NormalizedObservation

_BASE = "https://www.nhc.noaa.gov/CurrentStorms.json"

# NHC classification -> severity_norm
_CLASS_NORM = {
    "DB": 0.1, "LO": 0.15, "WV": 0.2, "EX": 0.25,
    "SD": 0.25, "SS": 0.25, "TD": 0.3, "TS": 0.35,
    "HU": 0.6, "TY": 0.6, "ST": 0.85,
}
_CLASS_LABEL = {
    "TD": "Tropical Depression", "TS": "Tropical Storm",
    "HU": "Hurricane", "TY": "Typhoon", "ST": "Super Typhoon",
    "EX": "Post-Tropical", "LO": "Low Pressure", "DB": "Disturbance",
}


class NhcAdapter(SourceAdapter):
    kind = "noaa_nhc"
    event_type = "tropical_cyclone"

    def fetch(self, timeout: int = 20, user_agent: str = "newsroom/0.1") -> FetchResult:
        from ...storage.repositories import utcnow
        req = urllib.request.Request(_BASE, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
        storms = doc.get("activeStorms") or []
        payloads = [json.dumps(s) for s in storms]
        return FetchResult(feed_url=_BASE, retrieved_at=utcnow(),
                           payloads=payloads)

    def split_items(self, body: str) -> Iterable[str]:
        yield body

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        s = json.loads(raw_payload)
        sid = s.get("id")
        name = s.get("name")
        if not sid or not name:
            return None
        cls = str(s.get("classification") or "TS").upper()
        try:
            intensity = int(s.get("intensity", 0))
        except (TypeError, ValueError):
            intensity = 0
        # Saffir-Simpson: >=74kt = hurricane; >=96kt = Cat3; >=137kt = Cat5
        if cls in ("HU", "TY"):
            if intensity >= 137:
                snorm = 1.0
            elif intensity >= 96:
                snorm = 0.85
            elif intensity >= 74:
                snorm = 0.6
            else:
                snorm = 0.4
        elif cls == "ST":
            snorm = 0.95
        else:
            snorm = _CLASS_NORM.get(cls, 0.3)
        label = _CLASS_LABEL.get(cls, cls)
        try:
            lat = float(s.get("latitudeNumeric"))
            lon = float(s.get("longitudeNumeric"))
        except (TypeError, ValueError):
            lat = lon = None
        try:
            pressure = int(s.get("pressure", 0))
        except (TypeError, ValueError):
            pressure = None
        advisory = s.get("publicAdvisory", {}) or {}
        if not isinstance(advisory, dict):
            advisory = {}
        fields = {
            "category": cls, "category_title": label,
            "storm_name": name, "storm_id": sid,
            "classification": cls,
            "severity": intensity, "magnitude_unit": "kts",
            "severity_norm": snorm,
            "pressure": pressure,
            "alert_level": "red" if snorm >= 0.85 else
                          "orange" if snorm >= 0.6 else "green",
            "advisory_num": advisory.get("advNum"),
            "advisory_issuance": advisory.get("issuance"),
            "occurred_at": s.get("lastUpdate") or "",
            "source_url": advisory.get("link") or
                          "https://www.nhc.noaa.gov/",
        }
        return NormalizedObservation(
            source="nhc", source_event_id=sid,
            event_type="tropical_cyclone",
            occurred_at=s.get("lastUpdate") or "",
            source_updated_at=s.get("lastUpdate"),
            latitude=lat, longitude=lon,
            location_label=(f"{abs(lat)}{'S' if lat < 0 else 'N'} "
                            f"{abs(lon)}{'W' if lon < 0 else 'E'}"
                            if lat is not None and lon is not None else ""),
            title=f"NHC {label} {name} — {intensity} kts"
                  + (f", {pressure} mb" if pressure else ""),
            severity=float(intensity), status="active",
            reviewed=True, fields=fields, cross_references=[])
