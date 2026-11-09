"""Broadcast frame — the 16:9 channel chrome around a vertical Short.

The Short is data; the frame is the channel. Rendered per program so the
gutters carry the story's title, impact chip, and provenance. The playout
overlays the scaled video at the frame's center window (607×1080 at x=656).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..visuals.renderer import (
    PALETTE, _FONT_CANDIDATES, _LEVEL_COLORS, _MONO_CANDIDATES, _font,
)

WINDOW_W = 608         # content window between header and ticker
WINDOW_H = 888         # y = 96..984
WINDOW_Y = 96
WINDOW_X = (1920 - WINDOW_W) // 2   # 656
TICKER_X = 40
TICKER_H = 88
TICKER_RESERVED_RIGHT = 340          # UTC clock + breathing room
TICKER_FONT_SIZE = 30
# Back-compat aliases for the playout scaler.
VIDEO_W, VIDEO_X = WINDOW_W, WINDOW_X


def ticker_geometry(width: int, height: int) -> tuple[int, int, int, int]:
    """x, y, readable viewport width, height for the lower-third ticker."""
    return (TICKER_X, height - TICKER_H,
            max(320, width - TICKER_X - TICKER_RESERVED_RIGHT), TICKER_H)


def render_ticker_strip(out: Path, text: str, viewport_width: int) -> Path:
    """Transparent ticker tile whose text starts just outside its viewport.

    FFmpeg scrolls this image across the lower third. The image width is
    viewport + margin + text, so `x = -mod(t, overlay_w)` wraps only after
    the last glyph has fully left the screen.
    """
    from PIL import Image, ImageDraw
    probe = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(probe)
    font = _font(_MONO_CANDIDATES, TICKER_FONT_SIZE)
    text = " ".join((text or "monitoring authoritative sources").split())
    margin = 40
    text_w = draw.textbbox((0, 0), text, font=font)[2]
    img = Image.new("RGBA", (viewport_width + margin + text_w, TICKER_H),
                    (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.text((viewport_width + margin, 24), text, font=font,
              fill=PALETTE["muted"])
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_frame(out: Path, width: int, height: int, *,
                 title: str, level: str = "NORMAL",
                 source_line: str = "", ticker: str | None = "",
                 detail_lines: list[str] | None = None,
                 live: bool = True) -> Path:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, height), PALETTE["bg"])
    draw = ImageDraw.Draw(img)

    # -- header --------------------------------------------------------
    # live=False → REPLAY badges in calm accent: filler never wears live-red.
    badge = "LIVE" if live else "REPLAY"
    badge_fill = PALETTE["red"] if live else PALETTE["accent"]
    draw.rectangle([0, 0, width, 88], fill=PALETTE["bg2"])
    chip = _font(_FONT_CANDIDATES, 40)
    live_w = draw.textbbox((0, 0), badge, font=chip)[2]
    draw.rounded_rectangle([40, 20, 40 + live_w + 40, 68], radius=10,
                           fill=badge_fill)
    draw.text((60, 28), badge, font=chip, fill=(255, 255, 255))
    draw.text((width - 40, 30), "NEWSROOM", font=_font(_MONO_CANDIDATES, 36),
              fill=PALETTE["muted"], anchor="ra")
    story_font = _font(_FONT_CANDIDATES, 34)
    # header center is visible — the video window starts at y=96
    draw.text((width // 2, 44), title[:60], font=story_font,
              fill=PALETTE["text"], anchor="mm")
    draw.line([0, 88, width, 88], fill=PALETTE["border"], width=2)

    # -- left gutter ----------------------------------------------------
    f_mono = _font(_MONO_CANDIDATES, 30)
    f_mid = _font(_FONT_CANDIDATES, 38)
    lx, ly = 56, 200
    draw.text((lx, ly), "NEWSROOM", font=f_mono, fill=PALETTE["muted"])
    big = badge
    big_font = _font(_FONT_CANDIDATES, 96)
    if draw.textbbox((0, 0), big, font=big_font)[2] > 520:
        big_font = _font(_FONT_CANDIDATES, 72)
    draw.text((lx, ly + 44), big, font=big_font, fill=badge_fill)
    draw.text((lx, ly + 160), "AUTONOMOUS", font=f_mid, fill=PALETTE["text"])
    draw.text((lx, ly + 210), "CRISIS DESK", font=f_mid, fill=PALETTE["text"])
    ly += 330
    draw.line([lx, ly, lx + 520, ly], fill=PALETTE["border"], width=2)
    ly += 40
    draw.text((lx, ly), "AUTHORITATIVE FEEDS", font=f_mono,
              fill=PALETTE["muted"])
    for i, src in enumerate(("USGS", "NWS", "EONET")):
        y = ly + 50 + i * 54
        draw.ellipse([lx, y + 8, lx + 20, y + 28], fill=PALETTE["green"])
        draw.text((lx + 34, y), src, font=f_mid, fill=PALETTE["text"])

    # -- right gutter ---------------------------------------------------
    rx = WINDOW_X + WINDOW_W + 56
    rw = width - rx - 48
    ry = 200
    lvl_color = _LEVEL_COLORS.get(level, PALETTE["accent"])
    lw = draw.textbbox((0, 0), level, font=chip)[2]
    draw.rounded_rectangle([rx, ry, rx + lw + 40, ry + 52], radius=10,
                           fill=lvl_color)
    draw.text((rx + 20, ry + 8), level, font=chip, fill=(255, 255, 255))
    ry += 96
    for line in _wrap(draw, title, f_mid, rw)[:4]:
        draw.text((rx, ry), line, font=f_mid, fill=PALETTE["text"])
        ry += 48
    ry += 24
    for line in detail_lines or []:
        draw.text((rx, ry), line, font=f_mono, fill=PALETTE["muted"])
        ry += 42
    if source_line:
        # If the source line is a URL, render it in a distinct box so
        # viewers can see/click the originating source. Otherwise it's
        # a plain attribution line.
        is_url = source_line.startswith("http")
        if is_url:
            url_display = source_line.replace("https://", "").replace("http://", "")
            if len(url_display) > 45:
                url_display = url_display[:42] + "…"
            box_w = draw.textbbox((0, 0), url_display, font=f_mono)[2] + 32
            draw.rounded_rectangle([rx, ry, rx + box_w, ry + 40],
                                   radius=6, fill=PALETTE["bg2"],
                                   outline=PALETTE["border"], width=1)
            draw.text((rx + 16, ry + 6), url_display, font=f_mono,
                      fill=PALETTE["accent"])
            ry += 56
        else:
            for line in _wrap(draw, source_line, f_mono, rw)[:2]:
                draw.text((rx, ry), line, font=f_mono, fill=PALETTE["muted"])
                ry += 42
    # window outline — the Short composites inside this panel
    draw.rectangle([WINDOW_X - 4, WINDOW_Y - 4,
                    WINDOW_X + WINDOW_W + 4, WINDOW_Y + WINDOW_H + 4],
                   outline=PALETTE["border"], width=2)
    ty_font = _font(_MONO_CANDIDATES, 24)
    for j, line in enumerate(
            _wrap(draw, "facts verified against the fact ledger",
                  ty_font, rw)[:2]):
        draw.text((rx, height - 180 + j * 34), line,
                  font=ty_font, fill=PALETTE["muted"])

    # -- ticker bar -----------------------------------------------------
    tx, ty, ticker_w, _ = ticker_geometry(width, height)
    draw.rectangle([0, ty, width, height], fill=PALETTE["bg2"])
    draw.line([0, ty, width, ty], fill=PALETTE["border"], width=2)
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    if ticker is not None:
        tick = ticker or "monitoring authoritative sources"
        while (tick and draw.textbbox((0, 0), tick, font=f_mono)[2]
               > ticker_w):
            tick = tick[:-2]
        draw.text((tx, ty + 24), tick, font=f_mono, fill=PALETTE["muted"])
    draw.text((width - 40, ty + 24), now, font=f_mono,
              fill=PALETTE["muted"], anchor="ra")

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out
