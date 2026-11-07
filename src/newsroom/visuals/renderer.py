"""Deterministic shot rendering (§19, §21).

Two backends behind one interface:

  CardRenderer     — Pillow data/location cards. Always available, fully
                     offline, deterministic. The sanctioned fallback (§37).
  MapLibreRenderer — visuals/maps/renderer.py: Playwright + MapLibre for
                     real geographic rendering when installed.

Both return one PNG (or a frame sequence) per shot; the composer turns them
into clips. Renderers never make editorial decisions.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Protocol

from ..models.fact import Fact
from ..models.shot import Shot, VisualPlan


class ShotRenderer(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...
    def render_shot(self, shot: Shot, plan: VisualPlan, out_dir: Path,
                    context: dict) -> Path: ...


# ------------------------------------------------------------ card renderer

PALETTE = {
    "bg": (11, 18, 32),          # deep navy
    "bg2": (17, 27, 46),
    "panel": (24, 37, 61),
    "border": (56, 74, 110),
    "text": (232, 238, 247),
    "muted": (148, 163, 190),
    "accent": (56, 189, 248),    # sky
    "red": (239, 68, 68),
    "amber": (245, 158, 11),
    "green": (52, 211, 153),
    "grid": (38, 52, 82),
}

_LEVEL_COLORS = {
    "FLASH": PALETTE["red"], "BREAKING": PALETTE["red"],
    "UPDATE": PALETTE["amber"], "NORMAL": PALETTE["accent"],
    "BACKGROUND": PALETTE["muted"],
}

_FONT_CANDIDATES = [
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
_MONO_CANDIDATES = [
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Courier.ttc",
]


def _font(candidates: list[str], size: int):
    from PIL import ImageFont
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


class CardRenderer:
    name = "cards"

    def available(self) -> tuple[bool, str]:
        try:
            from PIL import Image  # noqa: F401
            return True, "pillow"
        except ImportError:
            return False, "pillow missing"

    # -- public ------------------------------------------------------

    def render_shot(self, shot: Shot, plan: VisualPlan, out_dir: Path,
                    context: dict) -> Path:
        from PIL import Image, ImageDraw
        img = self._base(plan, context)
        draw = ImageDraw.Draw(img)
        handler = getattr(self, f"_shot_{shot.type}", self._shot_generic)
        handler(img, draw, shot, plan, context)
        out = out_dir / f"{shot.type}_{context.get('seq', 0):02d}.png"
        img.save(out)
        return out

    # -- frame furniture ----------------------------------------------

    def _base(self, plan: VisualPlan, context: dict):
        from PIL import Image, ImageDraw
        w, h = plan.width, plan.height
        img = Image.new("RGB", (w, h), PALETTE["bg"])
        draw = ImageDraw.Draw(img)
        # header band
        draw.rectangle([0, 0, w, 96], fill=PALETTE["bg2"])
        level = context.get("level", "BREAKING")
        chip_color = _LEVEL_COLORS.get(level, PALETTE["accent"])
        font_h = _font(_FONT_CANDIDATES, 44)
        draw.rounded_rectangle([40, 22, 40 + self._tw(draw, level, font_h) + 44, 74],
                               radius=12, fill=chip_color)
        draw.text((62, 32), level, font=font_h, fill=(255, 255, 255))
        draw.text((w - 40, 34), "NEWSROOM", font=_font(_MONO_CANDIDATES, 40),
                  fill=PALETTE["muted"], anchor="ra")
        # footer source strip
        draw.rectangle([0, h - 84, w, h], fill=PALETTE["bg2"])
        src = context.get("source_line", "")
        if src:
            draw.text((40, h - 64), src, font=_font(_MONO_CANDIDATES, 30),
                      fill=PALETTE["muted"])
        updated = context.get("updated", "")
        draw.text((w - 40, h - 64), updated, font=_font(_MONO_CANDIDATES, 30),
                  fill=PALETTE["muted"], anchor="ra")
        return img

    def _tw(self, draw, text: str, font) -> int:
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0]

    def _wrap(self, draw, text: str, font, max_w: int) -> list[str]:
        words, lines, cur = text.split(), [], ""
        for word in words:
            trial = f"{cur} {word}".strip()
            if self._tw(draw, trial, font) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines or [""]

    def _caption_safe_top(self, plan: VisualPlan) -> int:
        # Reserve lower ~28% of vertical frame for burned captions (§27).
        return int(plan.height * 0.68)

    # -- shot types ----------------------------------------------------

    def _shot_title_card(self, img, draw, shot, plan, context):
        f_big = _font(_FONT_CANDIDATES, 78)
        f_label = _font(_MONO_CANDIDATES, 34)
        y = int(plan.height * 0.30)
        label = context.get("event_label", "EARTHQUAKE")
        draw.text((60, y - 60), label.upper(), font=f_label, fill=PALETTE["accent"])
        for line in self._wrap(draw, shot.params.get("headline", ""), f_big,
                               plan.width - 120)[:4]:
            draw.text((60, y), line, font=f_big, fill=PALETTE["text"])
            y += 92
        if shot.params.get("location"):
            draw.text((60, y + 24), shot.params["location"],
                      font=_font(_FONT_CANDIDATES, 44), fill=PALETTE["muted"])

    def _shot_locator_map(self, img, draw, shot, plan, context):
        p = shot.params
        lat, lon = float(p["lat"]), float(p["lon"])
        x0, y0 = 60, 200
        x1 = plan.width - 60
        y1 = self._caption_safe_top(plan) - 40
        draw.rounded_rectangle([x0, y0, x1, y1], radius=18,
                               fill=PALETTE["panel"], outline=PALETTE["border"], width=2)

        span = 30.0  # degrees shown, equirectangular
        def project(la, lo):
            px = x0 + (lo - (lon - span / 2)) / span * (x1 - x0)
            py = y0 + ((lat + span / 2) - la) / span * (y1 - y0)
            return px, py

        # graticule every 5°
        f_grid = _font(_MONO_CANDIDATES, 22)
        grid_start_la = math.floor((lat - span / 2) / 5) * 5
        for g in range(int(grid_start_la), int(lat + span / 2) + 5, 5):
            _, py = project(g, lon)
            if y0 < py < y1:
                draw.line([x0 + 8, py, x1 - 8, py], fill=PALETTE["grid"], width=1)
                draw.text((x0 + 14, py - 24), f"{abs(g)}°{'N' if g >= 0 else 'S'}",
                          font=f_grid, fill=PALETTE["muted"])
        grid_start_lo = math.floor((lon - span / 2) / 5) * 5
        for g in range(int(grid_start_lo), int(lon + span / 2) + 5, 5):
            px, _ = project(lat, g)
            if x0 < px < x1:
                draw.line([px, y0 + 8, px, y1 - 8], fill=PALETTE["grid"], width=1)
                label = f"{abs(g)}°{'E' if g >= 0 else 'W'}"
                if px + 6 + self._tw(draw, label, f_grid) < x1 - 8:
                    draw.text((px + 6, y1 - 34), label, font=f_grid,
                              fill=PALETTE["muted"])

        # range rings (km -> approx degrees)
        cx, cy = project(lat, lon)
        for km in (100, 250, 500):
            r = km / 111.0 / span * (x1 - x0)
            draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                         outline=PALETTE["accent"], width=2)
            draw.text((cx + r * 0.72, cy - r * 0.72 - 14), f"{km} km",
                      font=f_grid, fill=PALETTE["accent"])
        # epicenter crosshair
        draw.ellipse([cx - 18, cy - 18, cx + 18, cy + 18], outline=PALETTE["red"], width=4)
        draw.line([cx - 34, cy, cx + 34, cy], fill=PALETTE["red"], width=3)
        draw.line([cx, cy - 34, cx, cy + 34], fill=PALETTE["red"], width=3)

        # labels
        f_lab = _font(_FONT_CANDIDATES, 42)
        if p.get("label"):
            for i, line in enumerate(self._wrap(draw, p["label"], f_lab, 520)):
                draw.text((x0 + 30, y0 + 30 + i * 50), line, font=f_lab,
                          fill=PALETTE["text"])
        coords = f"{abs(lat):.3f}°{'N' if lat >= 0 else 'S'}  {abs(lon):.3f}°{'E' if lon >= 0 else 'W'}"
        draw.text((x1 - 30, y0 + 30), coords, font=_font(_MONO_CANDIDATES, 30),
                  fill=PALETTE["muted"], anchor="ra")
        chips = []
        if p.get("magnitude"):
            chips.append(f"M {p['magnitude']}")
        if p.get("depth_km"):
            chips.append(f"DEPTH {p['depth_km']}")
        if p.get("tsunami"):
            chips.append("TSUNAMI FLAG")
        cxp = x0 + 30
        for chip in chips:
            color = PALETTE["red"] if "TSUNAMI" in chip else PALETTE["panel"]
            tw = self._tw(draw, chip, f_grid) + 28
            draw.rounded_rectangle([cxp, y1 - 48, cxp + tw, y1 - 14], radius=8,
                                   fill=color, outline=PALETTE["border"])
            draw.text((cxp + 14, y1 - 42), chip, font=f_grid,
                      fill=PALETTE["text"])
            cxp += tw + 12

    def _shot_metric_grid(self, img, draw, shot, plan, context):
        metrics = shot.params.get("metrics", [])[:6]
        f_label = _font(_MONO_CANDIDATES, 28)
        title = shot.params.get("title", "Event Data")
        draw.text((60, 150), title, font=_font(_FONT_CANDIDATES, 52),
                  fill=PALETTE["text"])
        cols, rows = 2, math.ceil(len(metrics) / 2)
        gx, gy = 40, 30
        cell_w = (plan.width - 80 - gx) // cols
        top = 240
        cell_h = min(210, (self._caption_safe_top(plan) - top - gy * rows) // max(rows, 1))
        for i, m in enumerate(metrics):
            c, r = i % cols, i // cols
            x = 40 + c * (cell_w + gx)
            y = top + r * (cell_h + gy)
            draw.rounded_rectangle([x, y, x + cell_w, y + cell_h], radius=16,
                                   fill=PALETTE["panel"], outline=PALETTE["border"], width=2)
            draw.text((x + 28, y + 22), m["label"], font=f_label, fill=PALETTE["muted"])
            value = str(m["value"]).upper()
            size = 64
            f_val = _font(_MONO_CANDIDATES, size)
            while size > 24 and self._tw(draw, value, f_val) > cell_w - 56:
                size -= 4
                f_val = _font(_MONO_CANDIDATES, size)
            draw.text((x + 28, y + cell_h // 2), value, font=f_val,
                      fill=PALETTE["text"])

    def _shot_revision_card(self, img, draw, shot, plan, context):
        f_title = _font(_FONT_CANDIDATES, 56)
        f_item = _font(_FONT_CANDIDATES, 44)
        draw.text((60, 170), f"Update — Revision {shot.params.get('revision')}",
                  font=f_title, fill=PALETTE["text"])
        draw.text((60, 250), "Fields revised by source since last report:",
                  font=_font(_FONT_CANDIDATES, 36), fill=PALETTE["muted"])
        y = 330
        for field in shot.params.get("changed", []):
            draw.text((80, y), "▸", font=f_item, fill=PALETTE["amber"])
            draw.text((130, y), field.replace("_", " ").upper(), font=f_item,
                      fill=PALETTE["text"])
            y += 66

    def _shot_source_card(self, img, draw, shot, plan, context):
        f_title = _font(_FONT_CANDIDATES, 52)
        f_item = _font(_MONO_CANDIDATES, 34)
        draw.text((60, 170), "Sources", font=f_title, fill=PALETTE["text"])
        y = 260
        for s in shot.params.get("sources", []):
            draw.text((60, y), s, font=f_item, fill=PALETTE["accent"])
            y += 52
        if shot.params.get("preliminary"):
            y += 20
            draw.rounded_rectangle([60, y, 60 + 420, y + 56], radius=10,
                                   fill=PALETTE["amber"])
            draw.text((80, y + 10), "PRELIMINARY REPORT",
                      font=_font(_MONO_CANDIDATES, 32), fill=(0, 0, 0))
        conf = shot.params.get("confidence")
        if conf is not None:
            draw.text((60, y + 100), f"source confidence {conf:.2f}",
                      font=f_item, fill=PALETTE["muted"])

    def _shot_generic(self, img, draw, shot, plan, context):
        draw.text((60, 200), shot.type.replace("_", " ").title(),
                  font=_font(_FONT_CANDIDATES, 60), fill=PALETTE["text"])


def render_plan(renderer: ShotRenderer, plan: VisualPlan, out_dir: Path,
                context: dict) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, shot in enumerate(plan.shots):
        ctx = dict(context, seq=i)
        paths.append(renderer.render_shot(shot, plan, out_dir, ctx))
    return paths
