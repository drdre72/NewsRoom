"""YouTube publishing — upload API for Shorts and long-form (§66).

No Google client library: raw HTTP via urllib (same as every other
adapter). Three pieces:
- OAuth 2.0 device flow → refresh token in .env (one-time `youtube auth`)
- Resumable upload (videos.insert) with title/description/chapters from
  the render manifest; gate-passed assets only
- "Best of the Day" playlist: highest-impact + most-aired stories

The LLM never decides what uploads. The publication gate is
deterministic, and `publishing.automatic` is the master switch.
"""
from __future__ import annotations

from .youtube import (
    authorize_desktop, save_refresh_token, refresh_access_token,
    upload_video, create_playlist, add_to_playlist,
    select_best_of_day, upload_eligible,
)

__all__ = [
    "authorize_desktop", "save_refresh_token", "refresh_access_token",
    "upload_video", "create_playlist", "add_to_playlist",
    "select_best_of_day", "upload_eligible",
]
