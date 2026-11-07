"""Standby slate — the broadcast's "monitoring sources" holding card.

Rendered fresh per standby block so the on-air clock is real. Same
palette/furniture as the card renderer so the slate doesn't look like an
error state.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..visuals.renderer import PALETTE, _FONT_CANDIDATES, _MONO_CANDIDATES, _font


def render_slate(out: Path, width: int, height: int,
                 headline: str = "MONITORING AUTHORITATIVE SOURCES") -> Path:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, height), PALETTE["bg"])
    draw = ImageDraw.Draw(img)

    # header band + LIVE chip
    draw.rectangle([0, 0, width, 96], fill=PALETTE["bg2"])
    font_chip = _font(_FONT_CANDIDATES, 44)
    tw = draw.textbbox((0, 0), "LIVE", font=font_chip)[2]
    draw.rounded_rectangle([48, 22, 48 + tw + 44, 74], radius=12,
                           fill=PALETTE["red"])
    draw.text((70, 32), "LIVE", font=font_chip, fill=(255, 255, 255))
    draw.text((width - 48, 34), "NEWSROOM",
              font=_font(_MONO_CANDIDATES, 40), fill=PALETTE["muted"],
              anchor="ra")

    # center card
    font_h = _font(_FONT_CANDIDATES, 72)
    font_sub = _font(_FONT_CANDIDATES, 40)
    cy = height // 2 - 80
    draw.text((width // 2, cy), headline, font=font_h,
              fill=PALETTE["text"], anchor="mm")
    draw.text((width // 2, cy + 90),
              "New verified events will interrupt this feed automatically.",
              font=font_sub, fill=PALETTE["muted"], anchor="mm")

    # pulsing-style status dots (static but alive-looking)
    for i, label in enumerate(("USGS", "NWS", "EONET")):
        x = width // 2 - 160 + i * 320
        draw.ellipse([x - 12, cy + 190, x + 12, cy + 214],
                     fill=PALETTE["green"])
        draw.text((x + 24, cy + 202), label, font=font_sub,
                  fill=PALETTE["muted"], anchor="lm")

    # footer: UTC clock + provenance note
    draw.rectangle([0, height - 84, width, height], fill=PALETTE["bg2"])
    f_foot = _font(_MONO_CANDIDATES, 30)
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    draw.text((48, height - 64), f"STANDBY {now}", font=f_foot,
              fill=PALETTE["muted"])
    draw.text((width - 48, height - 64),
              "no LLM on air · facts from authoritative feeds only",
              font=f_foot, fill=PALETTE["muted"], anchor="ra")

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out
