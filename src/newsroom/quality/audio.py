"""Audio QC wrapper for the publication gate (§47)."""
from __future__ import annotations


def check_render_audio(manifest: dict) -> dict:
    audio = manifest.get("audio", {})
    result = {"ok": True, "issues": []}
    if audio.get("engine") is None:
        # Silent bulletin is a degraded-but-valid render (§37).
        result["issues"].append("no narration (silent render)")
        result["degraded"] = True
    if not audio.get("hash") and audio.get("engine"):
        result["ok"] = False
        result["issues"].append("engine claimed but no audio hash")
    return result
