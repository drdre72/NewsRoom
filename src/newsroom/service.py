"""Newsroom core service — always-on ingestion + job drain (§38, §39).

    python main.py run --poll      # this loop
    launchd: com.newsroom.core     # same loop under RunAtLoad+KeepAlive

Discipline: poll sources on cadence, drain the render queue serially
(§5 memory model — one heavy job at a time on a 16 GB machine), exit
cleanly on SIGTERM/SIGINT so launchd can restart us. RAM holds no
persistent state; everything recoverable lives in SQLite (§39).
"""
from __future__ import annotations

import signal
import sqlite3
import time

from .config import Settings
from .logging import get_logger, log_event
from .models.job import JobStatus, JobType

log = get_logger("service")


class ServiceLoop:
    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        self.conn = conn
        self.settings = settings
        self._stop = False
        self._last_sweep = 0.0
        self._sweep_pending: list[str] | None = None

    def install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            log_event(log, 20, "shutdown signal received", signal=signum)
            self._stop = True
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    def run(self, interval: int) -> None:
        from .ingestion.polling import poll_once
        from .intelligence.source_health import record_poll
        from .pipeline import ingest_observation
        self.install_signal_handlers()
        log_event(log, 20, "core service started", interval=interval)
        while not self._stop:
            cycle_started = time.monotonic()
            try:
                report = poll_once(self.conn, self.settings, ingest_observation)
                record_poll(self.settings, report, conn=self.conn)
                if report["errors"]:
                    log_event(log, 30, "poll cycle had errors",
                              errors=len(report["errors"]))
            except Exception as exc:  # noqa: BLE001 — service must not die
                log_event(log, 40, "poll cycle failed", error=str(exc))
            # A backlog must not delay the next source poll until every
            # render finishes. One render at a time still preserves the
            # Apple Silicon memory budget.
            processed = self.drain_jobs(max_jobs=1)
            if not processed:
                # Idle cycle: an approved retention sweep may convert one
                # eligible video — render jobs always win the cycle.
                self._sweep_step()
            self._sleep(max(0, int(interval - (time.monotonic() - cycle_started))))
        log_event(log, 20, "core service stopped")

    def _sweep_step(self) -> None:
        """Auto-dehydrate, gated on storage.auto_dehydrate (off by
        default): at most max_dehydrate_per_sweep assets per interval,
        one per idle cycle, each verified before its MP4 is removed."""
        if not self.settings.get("storage.auto_dehydrate", False):
            return
        interval_h = float(
            self.settings.get("storage.sweep_interval_hours", 24))
        if self._sweep_pending is None:
            if time.time() - self._last_sweep < interval_h * 3600:
                return
            from .storage.recipes import retention_report
            budget = int(self.settings.get(
                "storage.max_dehydrate_per_sweep", 10))
            self._sweep_pending = [
                e["asset_id"]
                for e in retention_report(self.conn, self.settings)
                .get("eligible", [])][:budget]
            if not self._sweep_pending:
                self._last_sweep = time.time()
                self._sweep_pending = None
                return
            log_event(log, 20, "retention sweep started",
                      queued=len(self._sweep_pending))
        asset_id = self._sweep_pending.pop(0)
        try:
            from .storage.recipes import dehydrate
            dehydrate(self.conn, self.settings, execute=True,
                      verify=True, asset_ids=[asset_id])
        except Exception as exc:  # noqa: BLE001 — sweep never kills core
            log_event(log, 40, "retention sweep asset failed",
                      asset_id=asset_id, error=str(exc))
        if not self._sweep_pending:
            self._last_sweep = time.time()
            self._sweep_pending = None
            log_event(log, 20, "retention sweep complete")

    def _sleep(self, seconds: int) -> None:
        """Interruptible sleep — wake each second to check the stop flag."""
        for _ in range(seconds):
            if self._stop:
                return
            time.sleep(1)

    def drain_jobs(self, max_jobs: int | None = None) -> int:
        """Run queued jobs serially; optionally yield to the next poll.
        Returns the number of jobs processed this call."""
        from .storage import repositories as repo
        from .storage.retention import disk_health
        if not disk_health(self.settings)["ok"]:
            log_event(log, 40, "render paused: low disk space")
            return 0
        processed = 0
        while not self._stop:
            if max_jobs is not None and processed >= max_jobs:
                return processed
            job = repo.claim_next_job(self.conn)
            if not job:
                return processed
            processed += 1
            log_event(log, 20, "job claimed", job_id=job.job_id,
                      type=job.job_type, priority=job.priority,
                      event_id=job.event_id)
            try:
                self._run_job(job.job_type, job.event_id)
                repo.complete_job(self.conn, job.job_id)
                log_event(log, 20, "job done", job_id=job.job_id,
                          type=job.job_type)
            except Exception as exc:  # noqa: BLE001 — bad job ≠ dead service
                repo.retry_job(
                    self.conn, job.job_id, str(exc),
                    max_retries=int(self.settings.get(
                        "service.max_job_retries", 3)))
                log_event(log, 40, "job failed", job_id=job.job_id,
                          type=job.job_type, error=str(exc))
        return processed

    def _run_job(self, job_type: str, event_id: str | None) -> None:
        if job_type == JobType.SHORT_RENDER:
            from .render import render_short
            manifest = render_short(self.conn, self.settings, event_id)
            if manifest["publication_gate"]["verdict"] != "pass":
                raise RuntimeError(
                    f"render gate failed: {manifest['publication_gate']}")
            return
        if job_type == JobType.LONGFORM_RENDER:
            from .render import render_longform
            manifest = render_longform(self.conn, self.settings, event_id)
            if manifest["publication_gate"]["verdict"] != "pass":
                raise RuntimeError(
                    f"longform gate failed: {manifest['publication_gate']}")
            return
        raise ValueError(f"unknown job type {job_type}")
