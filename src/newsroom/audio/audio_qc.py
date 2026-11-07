"""Audio QC (§47) — cheap sanity checks before a render can publish."""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path


def check_audio(path: Path) -> dict:
    result = {"exists": path.exists(), "duration_s": 0.0, "clipping": None,
              "ok": False, "issues": []}
    if not path.exists():
        result["issues"].append("missing file")
        return result
    try:
        with wave.open(str(path)) as w:
            result["duration_s"] = w.getnframes() / w.getframerate()
            result["sample_rate"] = w.getframerate()
    except Exception as exc:  # noqa: BLE001
        result["issues"].append(f"unreadable wav: {exc}")
        return result
    if result["duration_s"] <= 0:
        result["issues"].append("zero duration")
    # ffmpeg volumedetect — clipping check without decoding in Python.
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    )
    for line in proc.stderr.splitlines():
        if "max_volume" in line:
            try:
                max_db = float(line.split("max_volume:")[1].strip().split()[0])
                result["clipping"] = max_db > -0.5
                if result["clipping"]:
                    result["issues"].append(f"near/at clipping ({max_db} dB)")
            except (ValueError, IndexError):
                pass
    result["ok"] = not result["issues"]
    return result
