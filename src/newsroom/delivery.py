from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .briefings import _hash, reconcile
from .config import Settings
from .editorial import inspect
from .storage import repositories as repo
from .storage.database import tx


class EditorialMailer(Protocol):
    def send_review(self, message: dict) -> str: ...


class AmbiguousDelivery(Exception):
    pass


class FakeMailer:
    def __init__(self, fail_once: bool = False):
        self.messages: list[dict] = []
        self.fail_once = fail_once

    def send_review(self, message: dict) -> str:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("fake transport failure")
        self.messages.append(message)
        return message["message_id"]


def drain_review(conn: sqlite3.Connection, mailer: EditorialMailer,
                 batch_size: int = 10, max_attempts: int = 5,
                 draft_id: str | None = None, revision_id: int | None = None) -> int:
    reconcile(conn)
    sent = 0
    rows = conn.execute(
        "SELECT o.* FROM editorial_outbox o JOIN briefing_drafts d ON d.draft_id=o.draft_id "
        "WHERE o.status='pending' AND o.next_attempt_at<=? "
        "AND d.status='READY_FOR_REVIEW' AND d.current_revision=o.revision_id "
        "AND (? IS NULL OR o.draft_id=?) AND (? IS NULL OR o.revision_id=?) "
        "ORDER BY o.created_at LIMIT ?",
        (repo.utcnow(), draft_id, draft_id, revision_id, revision_id,
         min(max(batch_size, 1), 20)),
    ).fetchall()
    for row in rows:
        revision = inspect(conn, row["draft_id"])["revision"]
        if _hash(revision["text"], revision["html"], revision["evidence"],
                 revision["event_refs"]) != revision["content_sha256"]:
            continue
        previous = conn.execute(
            "SELECT provider_receipt, status FROM editorial_outbox "
            "WHERE message_id=?", (row["in_reply_to"],)
        ).fetchone() if row["in_reply_to"] else None
        thread_id = None
        in_reply_to = None
        if previous and previous["status"] == "sent":
            in_reply_to = row["in_reply_to"]
            try:
                thread_id = json.loads(previous["provider_receipt"])["threadId"]
            except (TypeError, ValueError, KeyError):
                pass
        message = {
            "to": row["recipient"], "message_id": row["message_id"],
            "in_reply_to": in_reply_to, "thread_id": thread_id,
            "subject": f"[JARVINTELS REVIEW] [{inspect(conn, row['draft_id'])['draft']['tier']}] "
                       f"[{row['draft_id']}] READY",
            "body": "NOT PUBLISHED — EDITORIAL APPROVAL REQUIRED\n"
                    f"Draft: {row['draft_id']} revision: {row['revision_id']}\n"
                    f"SHA-256: {revision['content_sha256']}\n"
                    "Review with the local newsroom newsletter CLI; email replies never approve.\n"
                    f"Validation: {revision['validation_json']}\n"
                    f"Generated: {revision['created_at']}\n\n"
                    f"{revision['text']}\n\n"
                    "Independent public-source intelligence analysis. No government affiliation.",
            "html": revision["html"],
        }
        try:
            receipt = mailer.send_review(message)
        except AmbiguousDelivery:
            with tx(conn):
                conn.execute(
                    "UPDATE editorial_outbox SET attempts=attempts+1, status='uncertain' "
                    "WHERE idempotency_key=? AND status='pending'",
                    (row["idempotency_key"],),
                )
            continue
        except Exception:
            attempts = row["attempts"] + 1
            next_at = (datetime.now(timezone.utc) + timedelta(
                minutes=min(2 ** attempts, 60)
            )).isoformat().replace("+00:00", "Z")
            with tx(conn):
                conn.execute(
                    "UPDATE editorial_outbox SET attempts=?, status=?, next_attempt_at=? "
                    "WHERE idempotency_key=? AND status='pending'",
                    (attempts, "dead" if attempts >= max_attempts else "pending",
                     next_at, row["idempotency_key"]),
                )
            continue
        with tx(conn):
            conn.execute(
                "UPDATE editorial_outbox SET status='sent', attempts=attempts+1, "
                "provider_receipt=? WHERE idempotency_key=? AND status='pending'",
                (receipt, row["idempotency_key"]),
            )
        sent += 1
    return sent


def mock_dispatch(conn: sqlite3.Connection, settings: Settings, draft_id: str,
                  revision: int) -> str:
    reconcile(conn)
    draft = inspect(conn, draft_id)["draft"]
    if draft["status"] == "EXPIRED":
        return "stale"
    if draft["current_revision"] != revision or draft["status"] != "APPROVED":
        return "unapproved"
    if not settings.get("editorial.distribution_enabled", False):
        return "disabled"
    return "disabled"
