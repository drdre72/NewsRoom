"""YAML configuration loading.

Every tunable lives under config/. Code reads values through Settings.get
with dotted paths; nothing in the pipeline hardcodes model names, feed URLs,
thresholds, or paths.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .paths import config_dir

_FILES = ("settings", "sources", "thresholds", "broadcast", "models", "pronunciation")


class Settings:
    def __init__(self, merged: dict[str, Any], root: Path):
        self._data = merged
        self.root = root

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self._data.get(name, {})
        return value if isinstance(value, dict) else {}

    def raw(self) -> dict[str, Any]:
        return self._data


def _load_file(root: Path, name: str) -> dict[str, Any]:
    path = root / f"{name}.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    root = config_dir()
    merged: dict[str, Any] = {}
    for name in _FILES:
        merged = _deep_merge(merged, _load_file(root, name))
    # Environment overrides for the few values deployments legitimately change.
    if url := os.environ.get("NEWSROOM_LLM_BASE_URL"):
        merged.setdefault("llm", {})["base_url"] = url
    return Settings(merged, root)
