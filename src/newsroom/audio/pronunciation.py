"""Pronunciation overrides (§24, §25).

Applied to speech_text only — display text and captions are never corrupted
to satisfy the TTS engine. Matches are whole-word, case-sensitive on the
override key's first occurrence style ("NHC" matches "NHC" not "nhc").
"""
from __future__ import annotations

import re


def to_speech_text(display_text: str, overrides: dict[str, str]) -> str:
    if not overrides:
        return display_text
    out = display_text
    for word, spoken in overrides.items():
        pattern = re.compile(rf"(?<![\w/]){re.escape(word)}(?![\w/])")
        out = pattern.sub(spoken, out)
    return out
