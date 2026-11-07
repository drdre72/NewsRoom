"""Broadcast states (§32)."""
from __future__ import annotations

from enum import Enum


class BroadcastState(str, Enum):
    STARTING = "STARTING"
    NORMAL_PROGRAMMING = "NORMAL_PROGRAMMING"
    STORY = "STORY"
    BREAKING = "BREAKING"
    FLASH = "FLASH"
    TRANSITION = "TRANSITION"
    RECOVERY = "RECOVERY"
    DEGRADED = "DEGRADED"
    OFF_AIR = "OFF_AIR"
