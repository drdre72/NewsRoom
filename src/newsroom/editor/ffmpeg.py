"""FFmpeg wrapper (§28, §29).

All invocations are argv lists — never shell=True (§76). Encoder selection
prefers VideoToolbox hardware H.264 for thermal/power reasons (§29).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..config import Settings


def ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg not on PATH")
    return path


def ffprobe(path: Path) -> dict:
    import json
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    return json.loads(proc.stdout or "{}")


def available_encoders() -> set[str]:
    proc = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-encoders"],
        capture_output=True, text=True, timeout=30,
    )
    return set(proc.stdout.split())


def pick_encoder(settings: Settings) -> str:
    encoders = available_encoders()
    for name in settings.get("video.encoder_preference", ["h264_videotoolbox", "libx264"]):
        if name in encoders:
            return name
    raise RuntimeError("no usable H.264 encoder found")


def run(args: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    cmd = [ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {proc.stderr[-800:]}")
    return proc
