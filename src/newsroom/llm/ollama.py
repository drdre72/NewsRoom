"""Ollama client — stdlib HTTP, one job at a time (§11).

max_concurrent_jobs=1 by design: on a 16 GB Mac the model loads for a job
and releases. We use /api/generate with format=<json schema> so Ollama
constrains decoding to the schema server-side.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from ..config import Settings
from .client import LLMClient, LLMResult


class OllamaClient(LLMClient):
    provider = "ollama"

    def __init__(self, settings: Settings):
        self.base_url = settings.get("llm.base_url", "http://localhost:11434").rstrip("/")
        self.model = settings.get("llm.model") or self._pick_model(settings)
        self.timeout = int(settings.get("llm.timeout_seconds", 120))
        self.temperature = float(settings.get("llm.temperature", 0.2))

    def _pick_model(self, settings: Settings) -> str:
        """First preferred model that is actually pulled; else ''."""
        try:
            installed = {m["name"] for m in self._get("/api/tags").get("models", [])}
        except Exception:  # noqa: BLE001 — server may be down; treat as none
            return ""
        for want in settings.get("llm.preferred", []) or []:
            if want in installed:
                return want
            base = want.split(":")[0]
            match = next((m for m in installed
                          if m.split(":")[0] == base or m == base), None)
            if match:          # installed under a different tag of the family
                return match
        return next(iter(installed), "")

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=5) as resp:
            return json.loads(resp.read())

    def _post(self, path: str, body: dict, timeout: int) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def available(self) -> tuple[bool, str]:
        try:
            version = self._get("/api/version").get("version", "?")
        except Exception:  # noqa: BLE001
            return False, f"ollama server unreachable at {self.base_url}"
        if not self.model:
            return False, f"ollama up ({version}) but no model pulled"
        return True, f"ollama {version}, model={self.model}"

    def list_models(self) -> list[str]:
        try:
            return [m["name"] for m in self._get("/api/tags").get("models", [])]
        except Exception:  # noqa: BLE001
            return []

    def generate_json(
        self, prompt: str, schema: dict | None = None,
        system: str | None = None, timeout: int | None = None,
    ) -> LLMResult:
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": self.temperature},
            "format": schema if schema else "json",
        }
        if system:
            body["system"] = system
        t0 = time.monotonic()
        try:
            resp = self._post("/api/generate", body, timeout or self.timeout)
        except urllib.error.URLError as exc:
            return LLMResult(ok=False, error=f"ollama request failed: {exc}",
                             model=self.model)
        latency = int((time.monotonic() - t0) * 1000)
        text = resp.get("response", "")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return LLMResult(ok=False, text=text, model=self.model,
                             error="model returned non-JSON", latency_ms=latency)
        return LLMResult(
            ok=True, text=text, json=parsed, model=self.model,
            latency_ms=latency,
            usage={k: resp.get(k) for k in
                   ("eval_count", "prompt_eval_count", "total_duration") if k in resp},
        )
