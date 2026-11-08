"""GDELT Events adapter — global media event database (gdeltproject.org).

Polls the GDELT 2.0 Events export: `lastupdate.txt` points at the latest
~15-minute export zip; each TSV row is a CAMEO-coded media event with
actors, geography, Goldstein scale, and source counts. Only conflict-quad
rows (verbal/material) are normalized — cooperation/diplomatic noise is
stored as raw evidence but not reported.

severity_norm = min(1, mentions/150): GDELT's unit is coverage volume.
Shares `conflict_event` with ACLED so the two corroborate via fusion (§16).
"""
from __future__ import annotations

import io
import json
import urllib.parse
import urllib.request
import zipfile
from typing import Iterable

from .base import SourceAdapter, FetchResult
from ...models.observation import NormalizedObservation
from ...security import fetch_bytes, read_limited
from .nws import _iso

# GDELT v2 data files live on data.gdeltproject.org; api.* 404s.
_LASTUPDATE = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
_GDELT_HOSTS = {"data.gdeltproject.org", "api.gdeltproject.org"}

# GDELT 2.0 Events TSV column positions (0-indexed, per the codebook).
_C = dict(gid=0, day=1, actor1_name=6, actor1_country=7, actor2_name=16,
          actor2_country=17, is_root=25, code=26, cameo=27, quad=28,
          goldstein=29, mentions=30, sources=31, articles=32, tone=33,
          geo_name=51, geo_country=52, geo_adm1=53, geo_lat=55, geo_long=56,
          added=58, url=59)

_QUAD_TITLE = {"3": "Verbal Conflict", "4": "Material Conflict"}


class GdeltAdapter(SourceAdapter):
    kind = "gdelt"
    event_type = "conflict_event"

    def fetch(self, timeout: int = 20, user_agent: str = "newsroom/0.1") -> FetchResult:
        from ...storage.repositories import utcnow
        req = urllib.request.Request(_LASTUPDATE,
                                     headers={"User-Agent": user_agent})
        listing = fetch_bytes(req, timeout=timeout,
                              allowed_hosts=_GDELT_HOSTS,
                              max_bytes=64 * 1024).decode("utf-8")
        export_url = next(
            (ln.split()[-1] for ln in listing.splitlines()
             if ".export.CSV.zip" in ln), None)
        if not export_url:
            raise RuntimeError("no .export.CSV.zip in lastupdate.txt")
        # lastupdate.txt advertises http:// URLs — upgrade to https on the
        # approved host rather than weakening the fetch boundary.
        parts = urllib.parse.urlsplit(export_url)
        if parts.scheme == "http" and parts.hostname in _GDELT_HOSTS:
            export_url = urllib.parse.urlunsplit(
                ("https", parts.netloc, parts.path, "", ""))
        req = urllib.request.Request(export_url,
                                     headers={"User-Agent": user_agent})
        blob = fetch_bytes(req, timeout=timeout, allowed_hosts=_GDELT_HOSTS)
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            with zf.open(zf.namelist()[0]) as fh:
                tsv = read_limited(fh, 96 * 1024 * 1024).decode(
                    "utf-8", errors="replace")
        return FetchResult(feed_url=export_url, retrieved_at=utcnow(),
                           payloads=[tsv])

    def split_items(self, body: str) -> Iterable[str]:
        quads = {int(q) for q in
                 self.config.get("quad_classes", ["3", "4"])}
        min_src = int(self.config.get("min_num_sources", 2))
        limit = int(self.config.get("max_items", 300))
        emitted = 0
        for line in body.splitlines():
            if emitted >= limit:
                break
            cols = line.split("\t")
            if len(cols) < 60:
                continue
            try:
                if int(cols[_C["quad"]] or 0) not in quads:
                    continue            # live export zero-pads ("04")
            except ValueError:
                continue
            try:
                if int(cols[_C["sources"]] or 0) < min_src:
                    continue
            except ValueError:
                continue
            emitted += 1
            yield json.dumps(cols)          # compact row evidence

    def normalize(self, raw_payload: str) -> NormalizedObservation | None:
        cols = json.loads(raw_payload)
        if len(cols) < 60:
            return None
        gid = cols[_C["gid"]]
        if not gid:
            return None
        quad = cols[_C["quad"]]
        try:
            mentions = float(cols[_C["mentions"]] or 0)
        except ValueError:
            mentions = 0.0
        try:
            goldstein = float(cols[_C["goldstein"]] or 0)
        except ValueError:
            goldstein = 0.0
        a1 = cols[_C["actor1_name"]] or cols[_C["actor1_country"]] or "?"
        a2 = cols[_C["actor2_name"]] or cols[_C["actor2_country"]] or "?"
        geo = cols[_C["geo_name"]] or cols[_C["geo_country"]] or None
        fields = {
            "actor1": a1, "actor2": a2,
            "category": _QUAD_TITLE.get(quad, "Conflict"),
            "category_title": _QUAD_TITLE.get(quad, "Conflict"),
            "quad_class": quad,
            "event_code": cols[_C["code"]],
            "cameo_desc": cols[_C["cameo"]],
            "goldstein": goldstein,
            "num_mentions": cols[_C["mentions"]],
            "num_sources": cols[_C["sources"]],
            "num_articles": cols[_C["articles"]],
            "avg_tone": cols[_C["tone"]],
            "action_geo": geo,
            "action_country": cols[_C["geo_country"]],
            "admin1": cols[_C["geo_adm1"]],
            "is_root": cols[_C["is_root"]] == "1",
            "severity_norm": min(1.0, mentions / 150.0),
            "magnitude_unit": "mentions",
            "source_url": cols[_C["url"]],
        }
        return NormalizedObservation(
            source="gdelt",
            source_event_id=f"G{gid}",
            event_type="conflict_event",
            occurred_at=_iso(f"{cols[_C['added']][:4]}-{cols[_C['added']][4:6]}"
                             f"-{cols[_C['added']][6:8]}T{cols[_C['added']][8:10]}"
                             f":{cols[_C['added']][10:12]}:{cols[_C['added']][12:14]}Z") or "",
            source_updated_at=None,
            latitude=_f(cols[_C["geo_lat"]]),
            longitude=_f(cols[_C["geo_long"]]),
            location_label=geo,
            title=f"{a1} — {cols[_C['cameo']] or cols[_C['code']]} — {geo or gid}",
            severity=mentions or None,
            status="reported",
            reviewed=False,
            fields=fields,
            cross_references=([cols[_C["url"]]] if cols[_C["url"]] else []),
        )


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
