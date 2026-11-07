"""Environment verification (§54, §92).

    python main.py doctor

Produces a pass/warn/fail report for every production dependency. Missing
optional components degrade the pipeline (documented per §37); missing
required components fail the report.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import load_settings
from .paths import ensure_runtime_dirs, project_root

PASS, WARN, FAIL = "pass", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    required: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "required": self.required}


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.returncode, (out.stdout + out.stderr).strip()
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def _which(name: str) -> str | None:
    return shutil.which(name)


# ------------------------------------------------------------------- checks

def check_macos() -> Check:
    if sys.platform != "darwin":
        return Check("macOS", FAIL, f"platform is {sys.platform}; must be darwin", True)
    ver = platform.mac_ver()[0]
    return Check("macOS", PASS, ver, True)


def check_arm64() -> Check:
    machine = platform.machine()
    proc = subprocess.run(
        ["sysctl", "-in", "machdep.cpu.brand_string"],
        capture_output=True, text=True,
    ).stdout.strip() or machine
    if machine == "arm64":
        return Check("ARM64", PASS, f"native arm64 ({proc})", True)
    return Check("ARM64", FAIL, f"{machine} — production requires native Apple Silicon", True)


def check_python() -> Check:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 11)
    arm = platform.machine() == "arm64"
    status = PASS if ok and arm else (WARN if ok else FAIL)
    detail = f"{v.major}.{v.minor}.{v.micro} {platform.machine()} ({sys.executable})"
    return Check("Python", status, detail, True)


def check_sqlite() -> Check:
    return Check("SQLite", PASS, f"library {sqlite3.sqlite_version}", True)


def check_tool(name: str, required: bool) -> Check:
    path = _which(name)
    if not path:
        status = FAIL if required else WARN
        return Check(name, status, "not on PATH", required)
    rc, out = _run([path, "--version"])
    ver = out.splitlines()[0] if out else "installed (version unknown)"
    # x86-only binaries installed under Rosetta still answer, so verify arch.
    rc_a, arch_out = _run(["/usr/bin/arch", "-arm64", path, "--version"], timeout=10)
    if rc_a != 0 and rc == 0:
        return Check(name, WARN, f"{ver} — x86-only? (cannot run under -arm64)", required)
    return Check(name, PASS, f"{ver} @ {path}", required)


def check_ffmpeg() -> Check:
    path = _which("ffmpeg")
    if not path:
        return Check("FFmpeg", FAIL, "not on PATH — required for all media output", True)
    rc, out = _run([path, "-hide_banner", "-version"])
    first = out.splitlines()[0] if out else "ffmpeg"
    return Check("FFmpeg", PASS, first, True)


def check_videotoolbox() -> Check:
    path = _which("ffmpeg")
    if not path:
        return Check("VideoToolbox", FAIL, "ffmpeg missing", True)
    rc, out = _run([path, "-hide_banner", "-encoders"])
    if "h264_videotoolbox" in out:
        return Check("VideoToolbox", PASS, "h264_videotoolbox + hevc_videotoolbox available", True)
    return Check("VideoToolbox", FAIL, "no VideoToolbox encoders — cannot do efficient 24/7 encode", True)


def check_libass() -> Check:
    path = _which("ffmpeg")
    if not path:
        return Check("libass", FAIL, "ffmpeg missing", True)
    rc, out = _run([path, "-hide_banner", "-filters"])
    if re.search(r"\bass\b", out):
        return Check("libass", PASS, "ass subtitle burn filter present", True)
    return Check("libass", WARN, "no ass filter — captions will need alternative burn", False)


def check_ollama() -> Check:
    settings = load_settings()
    base = settings.get("llm.base_url", "http://localhost:11434")
    if not _which("ollama"):
        return Check("Ollama", WARN, "binary not found", False)
    try:
        with urllib.request.urlopen(f"{base}/api/version", timeout=3) as resp:
            version = json.loads(resp.read()).get("version", "?")
    except Exception:
        return Check("Ollama", WARN, f"installed but server not running at {base} (ollama serve)", False)
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=5) as resp:
            models = [m["name"] for m in json.loads(resp.read()).get("models", [])]
    except Exception:
        models = []
    detail = f"server {version} at {base}; models: {', '.join(models) if models else 'none pulled'}"
    return Check("Ollama", PASS if models else WARN, detail, False)


def check_playwright() -> Check:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return Check("Playwright", WARN, "python package not installed (uv sync --extra maps)", False)
    # Verify a Chromium build exists under the Playwright cache.
    cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    chromes = list(cache.glob("chromium-*")) if cache.exists() else []
    if chromes:
        return Check("Playwright", PASS, f"installed; Chromium at {chromes[-1]}", False)
    return Check("Playwright", WARN, "package installed; run `playwright install chromium`", False)


def check_whisper_cpp() -> Check:
    settings = load_settings()
    configured = settings.get("alignment.whisper_binary") or ""
    candidates = [configured] if configured else []
    candidates += ["whisper-cli", "whisper-cpp", "main"]
    for c in candidates:
        path = Path(c).expanduser() if os.sep in c else None
        found = str(path) if path and path.exists() else _which(c)
        if found:
            return Check("whisper.cpp", PASS, found, False)
    return Check("whisper.cpp", WARN, "not found — alignment will use estimated timings", False)


def check_tts() -> Check:
    settings = load_settings()
    order = settings.get("tts.backend_order", ["kokoro_mlx", "say"])
    from .audio.tts import backend_available  # late import keeps doctor light
    notes = []
    first_ok = None
    for name in order:
        ok, detail = backend_available(name, settings)
        notes.append(f"{name}: {'ok' if ok else 'unavailable'} ({detail})")
        if ok and first_ok is None:
            first_ok = name
    if first_ok is None:
        return Check("TTS", WARN, "no backend available; " + "; ".join(notes), False)
    status = PASS if first_ok == "kokoro_mlx" else WARN
    return Check("TTS", status, f"active={first_ok}; " + "; ".join(notes), False)


def check_disk() -> Check:
    usage = shutil.disk_usage(project_root())
    free_gb = usage.free / 1e9
    status = PASS if free_gb >= 20 else (WARN if free_gb >= 5 else FAIL)
    return Check("Disk space", status, f"{free_gb:.0f} GB free on {project_root()}", True)


def check_directories() -> Check:
    try:
        dirs = ensure_runtime_dirs()
        writable = all(os.access(d, os.W_OK) for d in dirs)
        status = PASS if writable else FAIL
        return Check("Directories", status, f"{len(dirs)} runtime dirs verified under {project_root()}", True)
    except Exception as exc:  # noqa: BLE001
        return Check("Directories", FAIL, str(exc), True)


def check_node() -> Check:
    path = _which("node")
    if not path:
        return Check("Node.js", WARN, "not on PATH — needed for Playwright/map tooling", False)
    rc, out = _run([path, "--version"])
    return Check("Node.js", PASS, out.splitlines()[0] if out else "installed", False)


# -------------------------------------------------------------------- report

_CHECKS = [
    check_macos,
    check_arm64,
    check_python,
    check_sqlite,
    lambda: check_tool("uv", required=False),
    check_node,
    check_ffmpeg,
    check_videotoolbox,
    check_libass,
    check_ollama,
    check_playwright,
    check_whisper_cpp,
    check_tts,
    check_disk,
    check_directories,
]


def run_doctor(json_out: bool = False) -> int:
    checks: list[Check] = []
    for fn in _CHECKS:
        try:
            checks.append(fn())
        except Exception as exc:  # noqa: BLE001 — doctor never crashes
            checks.append(Check(getattr(fn, "__name__", "check"), FAIL, f"check error: {exc}", True))

    failed = [c for c in checks if c.status == FAIL and c.required]
    warned = [c for c in checks if c.status == WARN]

    if json_out:
        print(json.dumps({
            "platform": "macos-arm64",
            "checks": [c.to_dict() for c in checks],
            "failed_required": len(failed),
            "warnings": len(warned),
        }, indent=2))
    else:
        print(f"\nnewsroom doctor — {project_root()}\n")
        width = max(len(c.name) for c in checks)
        icons = {PASS: "PASS", WARN: "warn", FAIL: "FAIL"}
        for c in checks:
            req = "*" if c.required else " "
            print(f"  [{icons[c.status]}]{req} {c.name:<{width}}  {c.detail}")
        print(f"\n  * = required for production runtime")
        print(f"  {len(checks) - len(failed) - len(warned)} passed, "
              f"{len(warned)} warnings, {len(failed)} required failures\n")
        if warned:
            print("  Warnings degrade capability per fallback policy (§37):")
            for c in warned:
                print(f"    - {c.name}: {c.detail}")
            print()
    return 1 if failed else 0
