"""Video QC — ffprobe verification of what FFmpeg actually wrote (§47)."""
from __future__ import annotations

from pathlib import Path

from ..editor.ffmpeg import ffprobe


def check_video(manifest: dict) -> dict:
    video = manifest.get("video", {})
    path = Path(video.get("path", ""))
    result = {"ok": False, "issues": []}
    if not path.exists():
        result["issues"].append("video file missing")
        return result
    try:
        probe = ffprobe(path)
    except Exception as exc:  # noqa: BLE001
        result["issues"].append(f"ffprobe failed: {exc}")
        return result
    streams = probe.get("streams", [])
    vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
    issues = result["issues"]
    if vstream.get("codec_name") != "h264":
        issues.append(f"codec {vstream.get('codec_name')} != h264")
    if (vstream.get("width"), vstream.get("height")) != (
            video.get("width"), video.get("height")):
        issues.append("dimensions mismatch")
    dur = float(probe.get("format", {}).get("duration", 0))
    if dur < 3.0:
        issues.append(f"suspicious duration {dur:.1f}s")
    if int(vstream.get("nb_frames", 0) or 0) < 30:
        issues.append("too few frames")
    result["duration_s"] = dur
    result["frames"] = vstream.get("nb_frames")
    result["ok"] = not issues
    return result
