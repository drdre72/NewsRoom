"""TTS abstraction (§23).

The pipeline asks for a TTSEngine; it never knows which backend synthesized
the audio. kokoro_mlx is the production target; `say` is the macOS-native
development fallback so the pipeline is verifiable with zero extra installs.
"""
from __future__ import annotations

import atexit
import json
import os
import select
import shutil
import subprocess
import sys
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

# ------------------------------------------------- resident kokoro worker

_READ_TIMEOUT_S = 300


class _KokoroWorker:
    """One resident mlx_audio process per model — the model loads once at
    spawn and serves every segment after. A dead worker respawns once;
    the caller falls back to the per-call CLI beyond that (§37)."""

    def __init__(self, model: str):
        self.model = model
        self._proc: subprocess.Popen | None = None

    def _spawn(self) -> None:
        src = Path(__file__).resolve().parents[2]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(src) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "newsroom.audio.kokoro_worker",
             "--model", self.model],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, text=True)
        # First spawn includes the model load — expensive but bounded.
        ready = self._read(timeout=180)
        if not ready or not ready.get("ready"):
            self._kill()
            raise RuntimeError(
                "kokoro worker failed to start: "
                f"{(ready or {}).get('error') or 'no handshake'}")

    def _read(self, timeout: float) -> dict | None:
        if not self._proc or not self._proc.stdout:
            return None
        r, _, _ = select.select([self._proc.stdout], [], [], timeout)
        if not r:
            return None
        line = self._proc.stdout.readline()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    def synthesize(self, text: str, voice: str, prefix: Path) -> list[Path]:
        for attempt in (0, 1):
            try:
                if self._proc is None or self._proc.poll() is not None:
                    self._spawn()
                assert self._proc and self._proc.stdin
                self._proc.stdin.write(json.dumps(
                    {"text": text, "voice": voice,
                     "file_prefix": str(prefix)}) + "\n")
                self._proc.stdin.flush()
                resp = self._read(timeout=_READ_TIMEOUT_S)
                if resp is None:
                    # A timed-out request stays in flight; its late response
                    # would desync the protocol. Kill the worker — the next
                    # attempt (or the CLI fallback) respawns clean.
                    self._kill()
                    raise RuntimeError("kokoro worker unresponsive")
                if resp.get("ok"):
                    return [Path(f) for f in resp["files"]]
                raise RuntimeError(
                    f"kokoro worker error: {resp.get('error') or 'unknown'}")
            except (BrokenPipeError, OSError, AssertionError) as exc:
                self._kill()
                if attempt:
                    raise RuntimeError(f"kokoro worker failed: {exc}")
        return []

    def _kill(self) -> None:
        if self._proc:
            self._proc.kill()
            self._proc = None


_WORKERS: dict[str, _KokoroWorker] = {}


def _worker_for(model: str) -> _KokoroWorker:
    worker = _WORKERS.get(model)
    if worker is None:
        worker = _WORKERS[model] = _KokoroWorker(model)
    return worker


def _close_workers() -> None:
    for worker in _WORKERS.values():
        worker._kill()


atexit.register(_close_workers)


class KokoroMlxEngine(TTSEngine):
    """Kokoro via mlx-audio (Apple Silicon). A resident worker process keeps
    the model loaded across segments and renders (§4 memory budget — one
    worker, killed on failure)."""
    name = "kokoro_mlx"

    def available(self) -> tuple[bool, str]:
        try:
            import mlx_audio  # noqa: F401
            return True, "mlx_audio installed"
        except ImportError:
            return False, "mlx_audio not installed (uv sync --extra tts)"

    def synthesize(self, display_text: str, speech_text: str, voice: str,
                   out_path: Path) -> AudioResult:
        model = self.settings.get(
            "tts.kokoro_model", "mlx-community/Kokoro-82M-v1.0-4bit")
        try:
            produced = _worker_for(model).synthesize(
                speech_text, voice, out_path.with_suffix(""))
        except RuntimeError:
            produced = self._synthesize_subprocess(
                model, speech_text, voice, out_path)
        if not produced:
            raise RuntimeError("kokoro synthesis failed: no audio produced")
        out_wav = out_path.with_suffix(".wav")
        if len(produced) == 1:
            produced[0].replace(out_wav)
        else:
            _concat_wavs(produced, out_wav)   # long text splits into parts
        return AudioResult(path=out_wav, duration_s=_wav_seconds(out_wav),
                           sample_rate=_wav_rate(out_wav), engine=self.name,
                           voice=voice)

    @staticmethod
    def _synthesize_subprocess(model: str, speech_text: str, voice: str,
                               out_path: Path) -> list[Path]:
        """Cold fallback: one-shot CLI, model loads per invocation. Used only
        when the resident worker can't serve (§37 degraded, not dead)."""
        # sys.executable = the venv interpreter that has mlx_audio; a bare
        # `python` would resolve to the system interpreter without it.
        cmd = [
            sys.executable, "-m", "mlx_audio.tts.generate",
            "--model", model,
            "--text", speech_text, "--voice", voice,
            "--file_prefix", str(out_path.with_suffix("")),
            "--audio_format", "wav",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        produced = sorted(out_path.parent.glob(
            f"{out_path.stem}_*.wav"))
        if proc.returncode != 0 or not produced:
            raise RuntimeError(f"kokoro synthesis failed: {proc.stderr[-400:]}")
        return produced


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


def _concat_wavs(parts: list[Path], out: Path) -> None:
    """Join kokoro's numbered output parts into one wav via ffmpeg concat."""
    listfile = out.with_suffix(".concat.txt")
    listfile.write_text(
        "".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(listfile), str(out)],
        check=True, capture_output=True, timeout=120)
    listfile.unlink(missing_ok=True)
    for p in parts:
        p.unlink(missing_ok=True)


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
