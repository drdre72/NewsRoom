"""launchd service management (§38-§40).

Plists are templates in macos/launchd/ with @TOKENS@ — never hand-edit the
installed copies; regenerate here and reinstall (MACOS_PRODUCTION.md).

    python -m newsroom.macos.install_services [--print]
    python -m newsroom.macos.uninstall_services
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from ..paths import logs_dir, project_root

LAUNCHD_DIR = Path(__file__).parent / "launchd"
LABELS = ("com.newsroom.core", "com.newsroom.broadcast", "com.newsroom.health")


def _substitutions() -> dict[str, str]:
    return {
        "@PYTHON@": sys.executable,
        "@ROOT@": str(project_root()),
        "@LOGS@": str(logs_dir()),
    }


def render_plist(template: Path) -> str:
    text = template.read_text(encoding="utf-8")
    for token, value in _substitutions().items():
        text = text.replace(token, value)
    # Validate the result is a real plist before it ever reaches LaunchAgents.
    plistlib.loads(text.encode())
    return text


def agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(labels: list[str] | None = None, print_only: bool = False) -> int:
    labels = labels or list(LABELS)
    dest = agents_dir()
    logs_dir().mkdir(parents=True, exist_ok=True)
    rc = 0
    for label in labels:
        template = LAUNCHD_DIR / f"{label}.plist"
        if not template.exists():
            print(f"missing template {template}")
            rc = 1
            continue
        text = render_plist(template)
        if print_only:
            print(f"===== {dest / template.name} =====")
            print(text)
            continue
        target = dest / template.name
        dest.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        # bootout first so reinstall is idempotent; ignore not-loaded errors.
        subprocess.run(["launchctl", "bootout", _domain(), str(target)],
                       capture_output=True)
        proc = subprocess.run(
            ["launchctl", "bootstrap", _domain(), str(target)],
            capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"{label}: bootstrap failed: {proc.stderr.strip()}")
            rc = 1
        else:
            print(f"{label}: installed and loaded → {target}")
    return rc


def uninstall(labels: list[str] | None = None) -> int:
    labels = labels or list(LABELS)
    dest = agents_dir()
    for label in labels:
        target = dest / f"{label}.plist"
        subprocess.run(["launchctl", "bootout", _domain(), label],
                       capture_output=True)
        if target.exists():
            target.unlink()
            print(f"{label}: unloaded and removed {target}")
        else:
            print(f"{label}: not installed")
    return 0


def status() -> int:
    for label in LABELS:
        proc = subprocess.run(
            ["launchctl", "print", f"{_domain()}/{label}"],
            capture_output=True, text=True)
        if proc.returncode == 0:
            state = next((l.split("=")[-1].strip() for l in
                          proc.stdout.splitlines() if "state =" in l), "?")
            pid = next((l.split("=")[-1].strip() for l in
                        proc.stdout.splitlines() if "pid =" in l), "-")
            print(f"{label}: loaded state={state} pid={pid}")
        else:
            print(f"{label}: not loaded")
    return 0
