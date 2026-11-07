"""Timeline assembly — Python decides structure, FFmpeg executes it (§28).

A Timeline is pure data: ordered visual segments (image or frame-sequence),
one narration track, caption items, output geometry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class VisualSegment:
    source: Path          # a PNG file OR a directory of f%05d.png frames
    duration_s: float
    is_sequence: bool = False


@dataclass
class Timeline:
    width: int
    height: int
    fps: int
    visuals: list[VisualSegment] = field(default_factory=list)
    audio: Path | None = None
    caption_items: list[tuple[float, float, str]] = field(default_factory=list)

    def video_duration(self) -> float:
        return sum(v.duration_s for v in self.visuals)
