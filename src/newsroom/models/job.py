"""Persistent job model (§71, §72).

SQLite-backed queue. Lower priority number == runs sooner; FLASH-class work
uses the smallest values so it preempts background renders.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class JobType(str):
    SCRIPT = "SCRIPT"
    TTS = "TTS"
    ALIGN = "ALIGN"
    MAP_RENDER = "MAP_RENDER"
    SHORT_RENDER = "SHORT_RENDER"
    LONGFORM_RENDER = "LONGFORM_RENDER"
    BROADCAST_PACKAGE = "BROADCAST_PACKAGE"


class JobStatus(str):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Priority bands (§72): smaller runs first.
class Priority(IntEnum):
    FLASH = 0
    BREAKING = 10
    LIVE_UPDATE = 20
    SHORT = 30
    NORMAL = 50
    LONGFORM = 70
    CACHE = 90


@dataclass
class Job:
    job_id: str
    job_type: str
    status: str
    priority: int
    event_id: str | None
    revision: int | None
    payload_json: str | None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    retry_count: int = 0
    error: str | None = None
