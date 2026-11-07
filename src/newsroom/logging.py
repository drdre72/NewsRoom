"""Structured JSON logging (§69).

Every component logs through get_logger(name); records are JSON lines on disk
(rotated) plus a compact human-readable console stream. Prose-only logs are
fine for humans but the machine-readable stream is the source of truth.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from typing import Any

from .paths import logs_dir

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "component": getattr(record, "component", record.name),
            "msg": record.getMessage(),
        }
        for key, value in getattr(record, "fields", {}).items():
            if key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now().strftime("%H:%M:%S")
        comp = getattr(record, "component", record.name)
        fields = getattr(record, "fields", {})
        extra = " ".join(f"{k}={v}" for k, v in fields.items())
        line = f"{ts} {record.levelname[:4]} {comp:<14} {record.getMessage()}"
        return f"{line}  {extra}" if extra else line


def configure(level: int = logging.INFO, json_file: bool = True) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger("newsroom")
    root.setLevel(level)
    root.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(_ConsoleFormatter())
    root.addHandler(console)

    if json_file:
        logs_dir().mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            logs_dir() / "newsroom.jsonl",
            maxBytes=20 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(_JsonFormatter())
        root.addHandler(file_handler)

    _CONFIGURED = True


class _Adapter(logging.LoggerAdapter):
    # stdlib process() REPLACES kwargs["extra"] with self.extra — per-call
    # fields from log_event were being silently dropped everywhere.
    def process(self, msg, kwargs):
        extra = dict(self.extra)
        extra.update(kwargs.pop("extra", None) or {})
        kwargs["extra"] = extra
        return msg, kwargs


def get_logger(component: str) -> logging.LoggerAdapter:
    configure()
    logger = logging.getLogger(f"newsroom.{component}")
    return _Adapter(logger, {"component": component})


def log_event(logger: logging.LoggerAdapter, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})
