"""Caption generation (§27).

SRT for sidecar use; ASS for burned-in captions via libass — stroke, shadow,
and a lower-third safe zone sized for 9:16 vertical video.
"""
from __future__ import annotations

from pathlib import Path


def _srt_ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_ts(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def write_srt(items: list[tuple[float, float, str]], path: Path) -> Path:
    lines = []
    for i, (start, end, text) in enumerate(items, 1):
        lines += [str(i), f"{_srt_ts(start)} --> {_srt_ts(end)}", text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _wrap_text(text: str, max_chars: int) -> str:
    """Manual wrap -> ASS \\N breaks so lines never exceed the safe zone."""
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if len(trial) <= max_chars:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return "\\N".join(lines) if lines else text


def write_ass(
    items: list[tuple[float, float, str]], path: Path,
    width: int = 1080, height: int = 1920,
) -> Path:
    vertical = height > width
    font_size = 58 if vertical else 46
    margin_v = int(height * 0.14)          # lower-third safe zone
    margin_lr = int(width * 0.06)
    # Helvetica ~0.55em advance per char -> chars/line inside the margins.
    max_chars = max(18, int((width - 2 * margin_lr) / (font_size * 0.55)))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Helvetica,{font_size},&H00FFFFFF,&H00FFFFFF,&H00101820,&H99000000,-1,0,0,0,100,100,0,0,1,4,3,2,{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for start, end, text in items:
        safe = _wrap_text(text.replace("\n", " "), max_chars)
        lines.append(
            f"Dialogue: 0,{_ass_ts(start)},{_ass_ts(end)},Caption,,0,0,0,,{safe}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
