"""MapLibre GL JS renderer — real geographic shots via Playwright (§20, §21).

Deterministic frame rendering, NOT screen recording: for animated shots we
evaluate the camera at each frame's timestamp, jump the map to it, wait for
idle, and screenshot. 10s @ 30fps = exactly 300 frames.

Requires: `uv sync --extra maps`, `playwright install chromium`, and
`maplibre-gl` under node_modules (see maps/web/). When any of that is
missing, available() reports False and the pipeline uses CardRenderer (§37).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from ...models.shot import Shot, VisualPlan

_WEB_DIR = Path(__file__).parent / "web"
_NODE_MODULES = Path(__file__).resolve().parents[4] / "node_modules"


class MapLibreRenderer:
    name = "maplibre"

    def __init__(self, jobs_per_browser: int = 25):
        self.jobs_per_browser = jobs_per_browser  # §22 bounded worker life
        self._jobs_done = 0
        self._pw = None
        self._browser = None
        self._page = None
        self._page_key: tuple | None = None

    def available(self) -> tuple[bool, str]:
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False, "playwright not installed (uv sync --extra maps)"
        ml = _NODE_MODULES / "maplibre-gl" / "dist" / "maplibre-gl.js"
        if not ml.exists():
            return False, "maplibre-gl not in node_modules (npm i maplibre-gl)"
        if not (_WEB_DIR / "map.html").exists():
            return False, "maps/web/map.html missing"
        return True, "playwright + maplibre ready"

    # -- browser lifecycle ----------------------------------------------

    def _ensure_page(self, plan: VisualPlan, context: dict):
        """One Chromium serves every shot in the render; it relaunches after
        jobs_per_browser shots (§22) or when the viewport changes."""
        key = (plan.width, plan.height)
        if (self._page is not None and self._page_key == key
                and self._jobs_done < self.jobs_per_browser):
            return self._page
        self.close()
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(args=["--hide-scrollbars"])
        self._page = self._browser.new_page(
            viewport={"width": plan.width, "height": plan.height})
        self._page.goto((_WEB_DIR / "map.html").as_uri())
        self._page.wait_for_function("window.__ready === true", timeout=15000)
        self._page.evaluate("(p) => window.__initMap(p)",
                            self._map_config(plan, context))
        self._page_key = key
        self._jobs_done = 0
        return self._page

    def close(self) -> None:
        for obj, method in ((self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:  # noqa: BLE001 — teardown is best-effort
                    pass
        self._pw = self._browser = self._page = self._page_key = None

    # -- shots ---------------------------------------------------------

    def render_shot(self, shot: Shot, plan: VisualPlan, out_dir: Path,
                    context: dict) -> Path:
        """Render one shot. Animated camera shots produce a frame sequence
        (dir of PNGs); static shots produce a single PNG."""
        frames = max(1, round(shot.duration_s * plan.fps))
        animate = shot.type in ("globe_to_location", "impact_radius") and frames > 1
        out_dir.mkdir(parents=True, exist_ok=True)

        page = self._ensure_page(plan, context)
        page.evaluate("(s) => window.__setShot(s)", json.dumps(shot.params))

        if not animate:
            page.wait_for_function("window.__idle === true", timeout=15000)
            out = out_dir / f"{shot.type}_{context.get('seq', 0):02d}.png"
            page.locator("#map").screenshot(path=str(out))
            self._frame(out, plan, context,
                        label=shot.params.get("label"))
            self._jobs_done += 1
            return out

        seq_dir = out_dir / f"seq_{shot.type}_{context.get('seq', 0):02d}"
        seq_dir.mkdir(exist_ok=True)
        for i in range(frames):
            t = i / max(1, frames - 1)
            page.evaluate("(t) => window.__evalCamera(t)", t)
            page.wait_for_function("window.__idle === true", timeout=15000)
            page.locator("#map").screenshot(
                path=str(seq_dir / f"f{i:05d}.png"))
        self._jobs_done += 1
        return seq_dir

    # -- map setup -------------------------------------------------------

    def _map_config(self, plan: VisualPlan, context: dict) -> str:
        """Fully offline style: painted background + vendored GeoJSON layers
        only. No public tile endpoints (§20)."""
        ml = _NODE_MODULES / "maplibre-gl" / "dist" / "maplibre-gl.js"
        sources: dict = {}
        layers: list = [{
            "id": "bg", "type": "background",
            "paint": {"background-color": "#0b1220"},
        }]
        land_path = _WEB_DIR / "assets" / "ne_110m_land.geojson"
        if land_path.exists():
            land = json.loads(land_path.read_text())
            sources["land"] = {"type": "geojson", "data": land}
            layers.append({
                "id": "land", "type": "fill", "source": "land",
                "paint": {"fill-color": "#18263f",
                          "fill-outline-color": "#3a5178"},
            })
        graticule = self._graticule()
        sources["grat"] = {"type": "geojson", "data": graticule}
        layers.append({
            "id": "grat", "type": "line", "source": "grat",
            "paint": {"line-color": "#263252", "line-width": 0.7},
        })
        style = {"version": 8, "sources": sources, "layers": layers}
        return json.dumps({"style": style, "maplibrePath": ml.as_uri(),
                           "interactive": False})

    @staticmethod
    def _graticule(step: int = 10) -> dict:
        feats = []
        for lon in range(-180, 181, step):
            feats.append({"type": "Feature", "geometry": {
                "type": "LineString",
                "coordinates": [[lon, -85], [lon, 85]]}})
        for lat in range(-80, 81, step):
            feats.append({"type": "Feature", "geometry": {
                "type": "LineString",
                "coordinates": [[-180, lat], [180, lat]]}})
        return {"type": "FeatureCollection", "features": feats}

    def _frame(self, png: Path, plan: VisualPlan, context: dict,
               label: str | None = None) -> None:
        """Drop the raw map capture into the standard frame furniture so map
        shots carry the same header/footer chrome as card shots."""
        from PIL import Image, ImageDraw
        from ..renderer import CardRenderer, _font, _FONT_CANDIDATES, PALETTE
        base = CardRenderer()._base(plan, context)
        map_img = Image.open(png).convert("RGB")
        top, bottom = 96, 84
        region = (0, top, plan.width, plan.height - bottom)
        base.paste(map_img.crop(region).resize(
            (plan.width, plan.height - bottom - top)), (0, top))
        # Locator shots carry a text label the map itself can't draw.
        if label:
            d = ImageDraw.Draw(base)
            d.rounded_rectangle([40, top + 20, 40 + 560, top + 84], radius=12,
                                fill=PALETTE["panel"])
            d.text((64, top + 32), label,
                   font=_font(_FONT_CANDIDATES, 42), fill=PALETTE["text"])
        base.save(png)
