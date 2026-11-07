"""LLM client interface (§11).

The LLM may summarize, organize, explain, and produce structured story JSON.
It may not create facts, change state, publish, or decide what airs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMResult:
    ok: bool
    text: str = ""
    json: dict[str, Any] | None = None
    model: str = ""
    error: str = ""
    latency_ms: int = 0
    usage: dict[str, Any] = field(default_factory=dict)


class LLMClient:
    provider = "base"

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        return []

    def generate_json(
        self, prompt: str, schema: dict | None = None,
        system: str | None = None, timeout: int | None = None,
    ) -> LLMResult:
        raise NotImplementedError
