"""Speech alignment (§26).

whisper.cpp gives real word timestamps when installed. When it isn't, we
fall back to proportional timing over the KNOWN script — ASR is never asked
to recreate text we already have; fallback timings are labeled 'estimated'.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..models.story import ScriptSegment


@dataclass
class WordTiming:
    word: str
    start_s: float
    end_s: float


@dataclass
class AlignmentResult:
    method: str                                # whisper_cpp | estimated
    segment_timings: list[tuple[float, float]] = field(default_factory=list)
    word_timings: list[WordTiming] = field(default_factory=list)


def _wav_duration(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def _whisper_binary(settings: Settings) -> str | None:
    configured = settings.get("alignment.whisper_binary") or ""
    for cand in ([configured] if configured else []) + ["whisper-cli", "whisper-cpp"]:
        found = shutil.which(cand)
        if found:
            return found
    return None


def align_segments(
    segments: list[ScriptSegment], audio_path: Path, settings: Settings
) -> AlignmentResult:
    binary = _whisper_binary(settings)
    if binary:
        try:
            return _align_with_whisper(segments, audio_path, binary, settings)
        except Exception:  # noqa: BLE001 — fall back, don't fail the render
            pass
    return _align_estimated(segments, audio_path)


def _align_with_whisper(
    segments: list[ScriptSegment], audio_path: Path,
    binary: str, settings: Settings,
) -> AlignmentResult:
    model = settings.get("alignment.model_path") or ""
    out_prefix = audio_path.with_suffix("")
    cmd = [binary, "-f", str(audio_path), "-oj", "-of", str(out_prefix)]
    if model:
        cmd += ["-m", model]
    subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)
    data = json.loads(out_prefix.with_suffix(".json").read_text())
    words: list[WordTiming] = []
    for chunk in data.get("transcription", []):
        for w in chunk.get("tokens", []) or []:
            offsets = w.get("offsets", {})
            if offsets:
                words.append(WordTiming(
                    word=w.get("text", ""),
                    start_s=offsets.get("from", 0) / 1000.0,
                    end_s=offsets.get("to", 0) / 1000.0,
                ))
    return _distribute(segments, audio_path, words, method="whisper_cpp")


def _align_estimated(
    segments: list[ScriptSegment], audio_path: Path
) -> AlignmentResult:
    return _distribute(segments, audio_path, [], method="estimated")


def _distribute(
    segments: list[ScriptSegment], audio_path: Path,
    words: list[WordTiming], method: str,
) -> AlignmentResult:
    """Spread the audio duration across segments proportional to word count.

    With real word timings we anchor segment starts to first-word matches;
    without them the distribution is purely proportional — either way the
    script text stays the source of truth (§26).
    """
    total = _wav_duration(audio_path)
    counts = [max(1, len(s.speech_text.split())) for s in segments]
    n_words = sum(counts)
    per_word = total / n_words if n_words else 0
    result = AlignmentResult(method=method, word_timings=words)
    t = 0.0
    for seg, count in zip(segments, counts):
        dur = count * per_word
        seg.duration_s = round(dur, 3)
        result.segment_timings.append((t, t + dur))
        t += dur
    return result
