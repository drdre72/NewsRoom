from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .briefings import _date, _hash, reconcile
from .storage import repositories as repo
from .storage.database import tx


def inspect(conn: sqlite3.Connection, draft_id: str) -> dict:
    draft = conn.execute("SELECT * FROM briefing_drafts WHERE draft_id=?", (draft_id,)).fetchone()
    if not draft:
        raise ValueError("draft not found")
    history = [
        {**dict(row), "evidence": json.loads(row["evidence_json"]),
         "event_refs": json.loads(row["event_refs_json"])}
        for row in conn.execute(
            "SELECT * FROM briefing_revisions WHERE draft_id=? ORDER BY revision_id",
            (draft_id,),
        )
    ]
    return {"draft": dict(draft), "revision": history[-1], "history": history}


def _action(conn: sqlite3.Connection, draft_id: str, revision: int,
            action: str, actor: str, note: str = "", destinations: list[str] | None = None) -> None:
    if not actor.strip():
        raise ValueError("verified operator identity required")
    with tx(conn):
        row = conn.execute(
            "SELECT d.status, d.current_revision, r.* FROM briefing_drafts d "
            "JOIN briefing_revisions r ON r.draft_id=d.draft_id AND r.revision_id=d.current_revision "
            "WHERE d.draft_id=?", (draft_id,),
        ).fetchone()
        if not row or row["current_revision"] != revision or row["status"] != "READY_FOR_REVIEW":
            raise ValueError("draft not ready for review at that revision")
        for ref in json.loads(row["event_refs_json"]):
            event = repo.get_event(conn, ref["event_id"])
            if not event or event.current_revision != ref["revision"]:
                raise ValueError("source revision changed during review")
        for fact in json.loads(row["evidence_json"]):
            rights = conn.execute(
                "SELECT commercial_publication_allowed, expires_at "
                "FROM source_rights_decisions WHERE source=?", (fact["source"],)
            ).fetchone()
            if not rights or not rights["commercial_publication_allowed"] or (
                rights["expires_at"] and _date(rights["expires_at"]) <= datetime.now(timezone.utc)
            ):
                raise ValueError("source rights changed during review")
        if _hash(row["text"], row["html"], json.loads(row["evidence_json"]),
                 json.loads(row["event_refs_json"])) != row["content_sha256"]:
            raise ValueError("draft content has changed")
        conn.execute(
            "INSERT INTO editorial_review_actions VALUES (?,?,?,?,?,?,?,?,?)",
            (repo.new_id("action"), draft_id, revision, row["content_sha256"],
             action, actor, note, json.dumps(destinations or []), repo.utcnow()),
        )
        conn.execute("UPDATE briefing_drafts SET status=? WHERE draft_id=?",
                     ({"approve": "APPROVED", "request_changes": "CHANGES_REQUESTED",
                       "reject": "REJECTED"}[action], draft_id))


def approve(conn: sqlite3.Connection, draft_id: str, revision: int,
            content_sha256: str, destinations: list[str], actor: str) -> None:
    reconcile(conn)
    if not destinations or set(destinations) != {"mock"}:
        raise ValueError("only mock delivery is supported; real publication is disabled")
    current = inspect(conn, draft_id)["revision"]
    if current["content_sha256"] != content_sha256:
        raise ValueError("exact revision hash mismatch")
    _action(conn, draft_id, revision, "approve", actor, destinations=destinations)


def request_changes(conn: sqlite3.Connection, draft_id: str, note: str, actor: str) -> None:
    if not note.strip():
        raise ValueError("change note required")
    revision = inspect(conn, draft_id)["draft"]["current_revision"]
    _action(conn, draft_id, revision, "request_changes", actor, note=note)


def reject(conn: sqlite3.Connection, draft_id: str, note: str, actor: str) -> None:
    revision = inspect(conn, draft_id)["draft"]["current_revision"]
    _action(conn, draft_id, revision, "reject", actor, note=note)
