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

    # -- shots ---------------------------------------------------------

    def render_shot(self, shot: Shot, plan: VisualPlan, out_dir: Path,
                    context: dict) -> Path:
        """Render one shot. Animated camera shots produce a frame sequence
        (dir of PNGs); static shots produce a single PNG."""
        from playwright.sync_api import sync_playwright

        frames = max(1, round(shot.duration_s * plan.fps))
        animate = shot.type in ("globe_to_location", "impact_radius") and frames > 1
        out_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--hide-scrollbars"])
            try:
                page = browser.new_page(viewport={"width": plan.width,
                                                  "height": plan.height})
                page.goto((_WEB_DIR / "map.html").as_uri())
                page.wait_for_function("window.__ready === true", timeout=15000)
                page.evaluate("(p) => window.__initMap(p)", self._map_config(plan, context))
                page.evaluate("(s) => window.__setShot(s)", json.dumps(shot.params))

                if not animate:
                    page.wait_for_function("window.__idle === true", timeout=15000)
                    out = out_dir / f"{shot.type}_{context.get('seq', 0):02d}.png"
                    page.locator("#map").screenshot(path=str(out))
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
            finally:
                browser.close()

    # -- map setup -------------------------------------------------------

    def _map_config(self, plan: VisualPlan, context: dict) -> str:
        """Fully offline style: painted background + GeoJSON layers only.
        No public tile endpoints (§20)."""
        ml = _NODE_MODULES / "maplibre-gl" / "dist" / "maplibre-gl.js"
        style = {
            "version": 8,
            "sources": {},
            "layers": [{
                "id": "bg", "type": "background",
                "paint": {"background-color": "#0b1220"},
            }],
        }
        return json.dumps({"style": style, "maplibrePath": ml.as_uri(),
                           "interactive": False})
