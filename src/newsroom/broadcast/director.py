"""Broadcast Director — the on-air decision loop (§31-§35).

States (§32): STARTING → NORMAL_PROGRAMMING ⇄ STORY/BREAKING/FLASH → OFF_AIR.
The loop: check for interrupt-eligible revisions → else run the rundown →
else standby slate. Every aired item is logged to air_log (auditable) and
mirrored into state.json for the preview/status endpoints.

The Director never invents content: programs are rendered, gate-passed
assets or a deterministic slate. The LLM has no authority here.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

from ..config import Settings
from ..logging import get_logger, log_event
from ..models.broadcast import BroadcastState
from ..paths import cache_dir
from ..storage import repositories as repo
from . import program as prog
from .playout import HlsPlayout
from .slate import render_slate

log = get_logger("director")


class BroadcastDirector:
    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        self.conn = conn
        self.settings = settings
        self.state = BroadcastState.STARTING
        self._stop = False
        self._interrupt_now = False
        self.playout = HlsPlayout(settings)
        self.state_file = self.playout.dir / "state.json"
        self._caffeinate: subprocess.Popen | None = None
        self._airing: tuple[str | None, int | None] = (None, None)
        self._airing_replay = False

    # -- lifecycle ---------------------------------------------------------
    def install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            log_event(log, 20, "director shutdown signal", signal=signum)
            self._stop = True
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    def run(self) -> None:
        self.install_signal_handlers()
        if not self._take_lock():
            log_event(log, 40, "another director holds the lock; exiting")
            raise SystemExit(1)
        self._hold_caffeinate()
        self._set_state(BroadcastState.NORMAL_PROGRAMMING, "on air (preview)")
        try:
            while not self._stop:
                self._tick()
        finally:
            self._set_state(BroadcastState.OFF_AIR, "shutdown")
            self._release_caffeinate()
        log_event(log, 20, "director off air")

    def _tick(self) -> None:
        interrupt = prog.pick_interrupt(self.conn, self.settings)
        if interrupt:
            self._air(interrupt)
            return
        program = prog.pick_next(self.conn, self.settings)
        if program:
            self._air(program)
            return
        replay = prog.pick_replay(self.conn, self.settings)
        if replay:
            self._air(replay)
        else:
            self._standby()

    # -- airing ------------------------------------------------------------
    def _air(self, program: prog.Program) -> None:
        is_interrupt = program.kind == "interrupt"
        state = {"FLASH": BroadcastState.FLASH,
                 "BREAKING": BroadcastState.BREAKING}.get(
            program.level, BroadcastState.STORY)
        self._set_state(state, program.title,
                        event_id=program.event_id, kind=program.kind)
        log_event(log, 20, "airing", kind=program.kind,
                  rundown=program.rundown_id, event_id=program.event_id,
                  revision=program.revision, impact=program.level)
        t0 = time.monotonic()
        self._airing = (program.event_id, program.revision)
        self._airing_replay = program.kind == "replay"
        frame = self._program_frame(program)
        self.playout.air_video(Path(program.asset_path), frame=frame)
        rc, err = self.playout.wait(stop_check=self._should_cut)
        dur = time.monotonic() - t0
        interrupted = self._interrupt_now
        self._interrupt_now = False
        self._airing = (None, None)
        self._airing_replay = False
        if rc != 0 and not interrupted and not self._stop:
            log_event(log, 40, "playout encode failed", error=err)
            # Ground the file: a corrupt/missing MP4 must leave every
            # picker or the channel retries it forever. A successful
            # re-render mints a fresh asset row that airs normally.
            blocked = repo.block_asset_path(
                self.conn, program.asset_path, err)
            log_event(log, 40, "asset blocked off air",
                      path=program.asset_path, rows=blocked)
            self._set_state(BroadcastState.DEGRADED,
                            f"encode error: {err[:120]}")
            return
        repo.log_air(self.conn, repo.new_id("air"), program.kind,
                     program.event_id, program.revision, program.rundown_id,
                     program.asset_path, round(dur, 2),
                     interrupted=interrupted)
        self._set_state(BroadcastState.NORMAL_PROGRAMMING, "between programs")

    def _program_frame(self, program: prog.Program) -> Path | None:
        """Per-program channel chrome — title/level/source baked in."""
        try:
            from .frame import render_frame
            w, h, _ = self.playout._geom()
            ev = repo.get_event(self.conn, program.event_id) \
                if program.event_id else None
            frame = cache_dir() / "frames" / \
                f"frame_{abs(hash((program.event_id, program.revision)))}.png"
            render_frame(
                frame, w, h,
                title=program.title,
                level=program.level or "NORMAL",
                source_line=(ev.source_ids[0] if ev and ev.source_ids
                             else "authoritative feed"),
                detail_lines=[
                    f"revision {program.revision}" if program.revision else "",
                    (ev.event_type.replace("_", " ") if ev else "")],
                ticker="monitoring authoritative feeds — "
                       "facts verified against the fact ledger",
                live=program.kind != "replay",
            )
            return frame
        except Exception as exc:  # noqa: BLE001 — frame is chrome, not content
            log_event(log, 30, "frame render failed; plain pillarbox",
                      error=str(exc))
            return None

    def _should_cut(self) -> bool:
        """Mid-program preemption (§33): only a *different* FLASH revision
        cuts a story in progress; a replay is filler and yields to any fresh
        programming. The airing event can't interrupt itself — it isn't in
        air_log until its air completes."""
        if self._stop:
            return True
        if self._interrupt_now:
            return True
        interrupt = prog.pick_interrupt(self.conn, self.settings)
        if (interrupt
                and (interrupt.event_id, interrupt.revision) != self._airing
                and (interrupt.level == "FLASH" or self._airing_replay)):
            self._interrupt_now = True
            log_event(log, 30, f"{interrupt.level} interrupt — "
                      "cutting program", event_id=interrupt.event_id)
            return True
        # Replays are filler: any fresh rundown item preempts mid-air.
        # The same event/revision finishing its replay is exempt — cutting
        # it just to re-air identical content as 'story' gains nothing.
        if self._airing_replay:
            nxt = prog.pick_next(self.conn, self.settings)
            if nxt and (nxt.event_id, nxt.revision) != self._airing:
                self._interrupt_now = True
                log_event(log, 20, "fresh rundown item preempts replay",
                          event_id=nxt.event_id)
                return True
        return False

    def _standby(self) -> None:
        standby_s = float(self.settings.get(
            "playout.standby_seconds", 20))
        p = self.settings.get("playout", {}) or {}
        slate = cache_dir() / "slate.png"
        render_slate(slate, int(p.get("width", 1920)), int(p.get("height", 1080)))
        self._set_state(BroadcastState.NORMAL_PROGRAMMING,
                        "standby — monitoring sources", kind="standby")
        t0 = time.monotonic()
        self.playout.air_slate(slate, standby_s)
        # standby yields to *anything* ready to air — a completed render
        # cuts the slate short within a poll interval, not after the full
        # slate duration
        def check():
            return (self._stop
                    or bool(prog.pick_interrupt(self.conn, self.settings))
                    or bool(prog.pick_next(self.conn, self.settings))
                    or bool(prog.pick_replay(self.conn, self.settings)))
        rc, err = self.playout.wait(stop_check=check)
        if rc == 0:
            repo.log_air(self.conn, repo.new_id("air"), "standby",
                         None, None, None, str(slate),
                         round(time.monotonic() - t0, 2),
                         interrupted=self._stop)

    # -- state / infra -------------------------------------------------------
    def _set_state(self, state: BroadcastState, detail: str,
                   event_id: str | None = None,
                   kind: str | None = None) -> None:
        self.state = state
        payload = {
            "state": state.value,
            "on_air": detail,
            "kind": kind,
            "event_id": event_id,
            "since": repo.utcnow(),
            "playlist": str(self.playout.playlist),
        }
        self.state_file.write_text(json.dumps(payload, indent=2))
        log_event(log, 20, "broadcast state", state=state.value, on_air=detail)

    def _take_lock(self) -> bool:
        """Single-director lock — a second `broadcast` process (manual run vs
        launchd KeepAlive) must never co-write live.m3u8."""
        import fcntl
        self._lock_fh = (self.playout.dir / "director.lock").open("w")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _hold_caffeinate(self) -> None:
        """Prevent system sleep while on air (§40) — assertion dies with us."""
        try:
            self._caffeinate = subprocess.Popen(
                ["caffeinate", "-s", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            log_event(log, 30, "caffeinate not found; sleep assertion skipped")

    def _release_caffeinate(self) -> None:
        if self._caffeinate:
            self._caffeinate.terminate()
            self._caffeinate = None
