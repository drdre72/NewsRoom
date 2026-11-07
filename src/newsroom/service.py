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

    def install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            log_event(log, 20, "shutdown signal received", signal=signum)
            self._stop = True
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    def run(self, interval: int) -> None:
        from .ingestion.polling import poll_once
        from .pipeline import ingest_observation
        self.install_signal_handlers()
        log_event(log, 20, "core service started", interval=interval)
        while not self._stop:
            try:
                report = poll_once(self.conn, self.settings, ingest_observation)
                if report["errors"]:
                    log_event(log, 30, "poll cycle had errors",
                              errors=len(report["errors"]))
            except Exception as exc:  # noqa: BLE001 — service must not die
                log_event(log, 40, "poll cycle failed", error=str(exc))
            self.drain_jobs()
            self._sleep(interval)
        log_event(log, 20, "core service stopped")

    def _sleep(self, seconds: int) -> None:
        """Interruptible sleep — wake each second to check the stop flag."""
        for _ in range(seconds):
            if self._stop:
                return
            time.sleep(1)

    def drain_jobs(self) -> None:
        """Run queued jobs serially until empty or stopped (§72)."""
        from .storage import repositories as repo
        while not self._stop:
            job = repo.claim_next_job(self.conn)
            if not job:
                return
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
