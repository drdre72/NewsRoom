"""TTS abstraction (§23).

The pipeline asks for a TTSEngine; it never knows which backend synthesized
the audio. kokoro_mlx is the production target; `say` is the macOS-native
development fallback so the pipeline is verifiable with zero extra installs.
"""
from __future__ import annotations

import shutil
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings


@dataclass
class AudioResult:
    path: Path
    duration_s: float
    sample_rate: int
    engine: str
    voice: str
    meta: dict = field(default_factory=dict)


class TTSEngine:
    name = "base"

    def __init__(self, settings: Settings):
        self.settings = settings

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    def synthesize(self, display_text: str, speech_text: str, voice: str,
                   out_path: Path) -> AudioResult:
        raise NotImplementedError


# ------------------------------------------------------------------- kokoro

class KokoroMlxEngine(TTSEngine):
    """Kokoro via mlx-audio (Apple Silicon). On-demand worker — loads the
    model per invocation and releases after (§4)."""
    name = "kokoro_mlx"

    def available(self) -> tuple[bool, str]:
        try:
            import mlx_audio  # noqa: F401
            return True, "mlx_audio installed"
        except ImportError:
            return False, "mlx_audio not installed (uv sync --extra tts)"

    def synthesize(self, display_text: str, speech_text: str, voice: str,
                   out_path: Path) -> AudioResult:
        # CLI invocation keeps us version-tolerant; wav lands beside out_path.
        cmd = [
            "python", "-m", "mlx_audio.tts.generate",
            "--text", speech_text, "--voice", voice,
            "--file_prefix", str(out_path.with_suffix("")),
            "--audio_format", "wav",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        produced = out_path.with_suffix(".wav")
        if proc.returncode != 0 or not produced.exists():
            raise RuntimeError(f"kokoro synthesis failed: {proc.stderr[-400:]}")
        return AudioResult(path=produced, duration_s=_wav_seconds(produced),
                           sample_rate=_wav_rate(produced), engine=self.name,
                           voice=voice)


# ------------------------------------------------------------------------ say

class SayEngine(TTSEngine):
    """macOS `say` — always present, arm64-native. Development/fallback
    backend; swap to kokoro_mlx for production narration (§23)."""
    name = "say"

    def available(self) -> tuple[bool, str]:
        if shutil.which("say"):
            return True, "macOS speech synthesis"
        return False, "say not found (non-macOS?)"

    def synthesize(self, display_text: str, speech_text: str, voice: str,
                   out_path: Path) -> AudioResult:
        say_voice = self.settings.get("tts.say_voice", "Samantha")
        aiff = out_path.with_suffix(".aiff")
        wav = out_path.with_suffix(".wav")
        subprocess.run(
            ["say", "-v", voice or say_voice, "-o", str(aiff), speech_text],
            check=True, capture_output=True, timeout=120,
        )
        # Normalize to 22.05k mono wav for alignment + mixing.
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(aiff), "-ar", "22050", "-ac", "1", str(wav)],
            check=True, capture_output=True, timeout=60,
        )
        aiff.unlink(missing_ok=True)
        return AudioResult(path=wav, duration_s=_wav_seconds(wav),
                           sample_rate=_wav_rate(wav), engine=self.name,
                           voice=voice or say_voice)


_BACKENDS = {"kokoro_mlx": KokoroMlxEngine, "say": SayEngine}


def backend_available(name: str, settings: Settings) -> tuple[bool, str]:
    cls = _BACKENDS.get(name)
    if not cls:
        return False, f"unknown backend '{name}'"
    try:
        return cls(settings).available()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def select_engine(settings: Settings) -> TTSEngine | None:
    for name in settings.get("tts.backend_order", ["kokoro_mlx", "say"]):
        cls = _BACKENDS.get(name)
        if not cls:
            continue
        engine = cls(settings)
        ok, _ = engine.available()
        if ok:
            return engine
    return None


def _wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path)) as w:
            return round(w.getnframes() / w.getframerate(), 3)
    except Exception:  # noqa: BLE001
        return 0.0


def _wav_rate(path: Path) -> int:
    try:
        with wave.open(str(path)) as w:
            return w.getframerate()
    except Exception:  # noqa: BLE001
        return 0
