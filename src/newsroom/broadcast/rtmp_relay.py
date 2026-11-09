"""Persistent RTMP relay — one continuous push to YouTube (§34, §66, §77).

Per-program encodes each own a short-lived ffmpeg; teeing RTMP there
bounced the YouTube connection at every program boundary, which ingest
flags as "not receiving enough video." This relay is a single long-lived
ffmpeg that follows the live HLS window (live.m3u8 grows forever, no
ENDLIST) and remuxes the already-encoded h264/aac segments to FLV with
`-c copy` — near-zero CPU, and the TCP session survives program joins,
discontinuities, and even Director restarts.

Guards:
- Same three gates as the old tee: outputs.rtmp.enabled AND
  publishing.automatic AND the env var (§66, §77).
- Stale-window guard: only streams while the Director is producing
  segments; a dead playlist never re-airs its tail to YouTube.
- The stream key lives in the URL in argv only (unavoidable for RTMP);
  stderr is scrubbed before logging so the key never reaches logs.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path

from ..config import Settings
from ..editor import ffmpeg
from ..logging import get_logger, log_event
from ..paths import resolve
from ..security import redact_error

log = get_logger("rtmp_relay")

_STALL_SECONDS = 45        # no fresh segment this long → Director is off air
_RETRY_SECONDS = 5         # backoff between respawns (doubles to 60 max)
_STABLE_SECONDS = 60       # a run this long resets the backoff


def rtmp_url(settings: Settings) -> str | None:
    """All three publication gates must pass before a URL is handed out."""
    rtmp = settings.get("outputs.rtmp", {}) or {}
    if not rtmp.get("enabled"):
        return None
    if not settings.get("publishing.automatic", False):
        return None
    url = os.environ.get(rtmp.get("url_env", "NEWSROOM_RTMP_URL"), "")
    return url or None


def _ffmpeg_args(playlist: Path, url: str) -> list[str]:
    """Read the live playlist near its edge. Video is copied; audio is
    re-encoded through aresample=async so each program boundary's AAC
    priming gap is filled — otherwise every join clicks/stutters on
    YouTube. rw_timeout recycles the ffmpeg when the write stalls."""
    return [
        ffmpeg.ffmpeg_path(), "-hide_banner", "-loglevel", "error",
        "-re", "-live_start_index", "-3", "-i", str(playlist),
        "-c:v", "copy",
        "-af", "aresample=async=1:first_pts=0",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-rw_timeout", "15000000",
        "-f", "flv", url,
    ]


def _newest_segment(hls_dir: Path) -> float:
    """mtime of the newest seg_*.ts, or 0 when the window is empty."""
    newest = 0.0
    for seg in hls_dir.glob("seg_*.ts"):
        newest = max(newest, seg.stat().st_mtime)
    return newest


class RtmpRelay:
    def __init__(self, settings: Settings):
        self.settings = settings
        hls = settings.get("playout.hls", {}) or {}
        self.hls_dir = resolve(hls.get("dir", "output/preview"))
        self.playlist = self.hls_dir / hls.get("playlist", "live.m3u8")
        rtmp = settings.get("outputs.rtmp", {}) or {}
        self.stall_seconds = int(rtmp.get("stall_seconds", _STALL_SECONDS))
        self._stop = False
        self._proc: subprocess.Popen | None = None

    def install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            log_event(log, 20, "rtmp relay shutdown signal", signal=signum)
            self._stop = True
            self._terminate()
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    def _terminate(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _programming_live(self) -> bool:
        return (self.playlist.exists()
                and time.time() - _newest_segment(self.hls_dir)
                < self.stall_seconds)

    def run(self) -> int:
        self.install_signal_handlers()
        url = rtmp_url(self.settings)
        if not url:
            log_event(log, 40, "rtmp relay gated off — needs "
                      "outputs.rtmp.enabled + publishing.automatic + "
                      "url_env; exiting")
            return 1
        log_event(log, 20, "rtmp relay starting", playlist=self.playlist)
        backoff = _RETRY_SECONDS
        while not self._stop:
            if not self._programming_live():
                time.sleep(5)
                continue
            started = time.time()
            rc = self._stream(url)
            if self._stop:
                break
            backoff = (_RETRY_SECONDS if time.time() - started
                       > _STABLE_SECONDS else min(backoff * 2, 60))
            log_event(log, 30, "relay ffmpeg exited; respawning",
                      rc=rc, backoff_seconds=backoff)
            time.sleep(backoff)
        self._terminate()
        return 0

    def _stream(self, url: str) -> int:
        self._proc = subprocess.Popen(
            _ffmpeg_args(self.playlist, url),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        log_event(log, 20, "relay pushing", pid=self._proc.pid)
        while self._proc.poll() is None:
            if self._stop:
                break
            # Playlist went quiet → we would only re-air the stale tail.
            if not self._programming_live():
                log_event(log, 30, "HLS window stale; pausing relay")
                break
            time.sleep(2)
        if self._proc.poll() is None:
            self._proc.terminate()
        _, err = self._proc.communicate()
        if err.strip():
            log_event(log, 30, "relay ffmpeg stderr",
                      detail=redact_error(Exception(err))[-400:])
        return self._proc.returncode or 0


def run(settings: Settings) -> int:
    return RtmpRelay(settings).run()
