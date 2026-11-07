"""Media composer (§28) — Timeline -> ffmpeg -> MP4.

Python emits the structure; FFmpeg does the pixels. One pass: per-segment
inputs -> normalize -> concat -> ASS burn -> hardware encode.
"""
from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..logging import get_logger, log_event
from . import ffmpeg
from .timeline import Timeline

log = get_logger("composer")


def compose_short(
    timeline: Timeline,
    out_path: Path,
    settings: Settings,
    ass_path: Path | None = None,
    encoder: str | None = None,
) -> Path:
    if not timeline.visuals:
        raise ValueError("timeline has no visual segments")
    encoder = encoder or ffmpeg.pick_encoder(settings)
    w, h, fps = timeline.width, timeline.height, timeline.fps

    args: list[str] = []
    for seg in timeline.visuals:
        if seg.is_sequence:
            args += ["-framerate", str(fps), "-i", str(seg.source / "f%05d.png")]
        else:
            args += ["-loop", "1", "-framerate", str(fps), "-t",
                     f"{seg.duration_s:.3f}", "-i", str(seg.source)]
    audio_index = None
    if timeline.audio:
        audio_index = len(timeline.visuals)
        args += ["-i", str(timeline.audio)]

    chains = []
    labels = []
    for i in range(len(timeline.visuals)):
        chains.append(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,fps={fps}[v{i}]")
        labels.append(f"[v{i}]")
    chains.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[vcat]")
    last = "vcat"
    if ass_path:
        escaped = str(ass_path).replace("'", r"\'").replace(":", r"\:")
        chains.append(f"[vcat]ass='{escaped}'[vout]")
        last = "vout"
    filtergraph = ";".join(chains)

    args += ["-filter_complex", filtergraph, "-map", f"[{last}]"]
    if audio_index is not None:
        args += ["-map", f"{audio_index}:a"]

    args += ["-c:v", encoder, "-pix_fmt", "yuv420p"]
    if encoder == "h264_videotoolbox":
        args += ["-b:v", "8M"]
    else:
        args += ["-crf", "20", "-preset", "medium"]
    if audio_index is not None:
        args += ["-c:a", settings.get("video.audio_codec", "aac"),
                 "-b:a", settings.get("video.audio_bitrate", "160k")]
    args += ["-t", f"{timeline.video_duration():.3f}",
             "-movflags", "+faststart", str(out_path)]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    log_event(log, 20, "composing", out=str(out_path), encoder=encoder,
              segments=len(timeline.visuals), duration=timeline.video_duration())
    ffmpeg.run(args)
    return out_path
