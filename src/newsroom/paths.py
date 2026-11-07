"""Filesystem locations for the project.

Everything resolves from the project root (the directory containing
pyproject.toml), so services running under launchd behave identically to
interactive shells as long as they use NEWSROOM_ROOT or the repo checkout.
"""
from __future__ import annotations

import os
from pathlib import Path

_ENV_ROOT = "NEWSROOM_ROOT"


def project_root() -> Path:
    """Locate the project root (dir containing pyproject.toml)."""
    env = os.environ.get(_ENV_ROOT)
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    # Fallback: two levels up from src/newsroom/paths.py
    return here.parents[2]


def config_dir() -> Path:
    env = os.environ.get("NEWSROOM_CONFIG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return project_root() / "config"


def resolve(path: str | Path) -> Path:
    """Resolve a configured path: absolute stays absolute, else root-relative."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else (project_root() / p)


def db_path() -> Path:
    env = os.environ.get("NEWSROOM_DB")
    if env:
        return Path(env).expanduser().resolve()
    return resolve("newsroom.db")


def logs_dir() -> Path:
    return resolve("logs")


def cache_dir() -> Path:
    return resolve("cache")


def output_dir() -> Path:
    return resolve("output")


def ensure_runtime_dirs() -> list[Path]:
    dirs = [
        logs_dir(),
        cache_dir(),
        output_dir() / "shorts",
        output_dir() / "longform",
        output_dir() / "broadcast",
        output_dir() / "manifests",
        cache_dir() / "frames",
        cache_dir() / "audio",
        cache_dir() / "shots",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    return dirs
