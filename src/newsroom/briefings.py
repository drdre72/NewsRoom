from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .config import Settings, load_settings
from .storage import repositories as repo
from .storage.database import tx

_FIELDS = ("title", "location_label", "magnitude", "alert_event", "instruction")
def _state_name(code: str) -> str:
    names = (
        "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|"
        "Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|"
        "Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|"
        "Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|"
        "New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|"
        "Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|"
        "Virginia|Washington|West Virginia|Wisconsin|Wyoming"
    )
    return dict(zip(
        "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
        "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split(),
        names.split("|"),
    )).get(code.upper(), code)


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _url(value: str, host: str) -> bool:
    try:
        parsed = urlsplit(value)
        if not host or host.lower() == "localhost" or host.lower().endswith(".localhost"):
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        return (parsed.scheme == "https" and parsed.hostname == host
                and not parsed.username and not parsed.password
                and parsed.port in (None, 443))
    except ValueError:
        return False


def _source_link(payload: str, feed_url: str, host: str) -> str:
    try:
        data = json.loads(payload)
        candidates = (
            data.get("properties", {}).get("url"),
            data.get("properties", {}).get("@id"),
            data.get("source_url"),
        )
        for candidate in candidates:
            if isinstance(candidate, str) and _url(candidate, host):
                return candidate
    except (ValueError, TypeError, AttributeError):
        pass
    return feed_url


def record_rights(conn: sqlite3.Connection, source: str, reviewer: str,
                  terms_url: str, expires_at: str | None = None) -> None:
    try:
        host = urlsplit(terms_url).hostname or ""
    except ValueError:
        host = ""
    if not source or not reviewer or not _url(terms_url, host):
        raise ValueError("source, reviewer and HTTPS terms URL required")
    with tx(conn):
        conn.execute(
            "INSERT INTO source_rights_decisions "
            "(source, ingest_allowed, summary_allowed, commercial_publication_allowed, "
            "terms_url, reviewed_by, reviewed_at, expires_at) VALUES (?,1,1,1,?,?,?,?) "
            "ON CONFLICT(source) DO UPDATE SET ingest_allowed=1, summary_allowed=1, "
            "commercial_publication_allowed=1, terms_url=excluded.terms_url, "
            "reviewed_by=excluded.reviewed_by, reviewed_at=excluded.reviewed_at, "
            "expires_at=excluded.expires_at",
            (source, terms_url, reviewer, repo.utcnow(), expires_at),
        )


def _eligible(conn: sqlite3.Connection, settings: Settings, tier: str, profile: dict) -> list[dict]:
    now = datetime.now(timezone.utc)
    age = timedelta(hours=int(settings.get("editorial.max_age_hours", 48)))
    selected = []
    for event in repo.list_events(conn, limit=100):
        if event.status in ("cancelled", "resolved") or event.event_type in (
            "crime_report", "conflict_event", "protest", "news_report"
        ):
            continue
        if not event.current_revision or event.confidence_score < float(
            settings.get("confidence.corroborate_min", 0.70)
        ):
            continue
        if now - _date(event.last_updated) > age:
            continue
        label = event.location_label or ""
        latitude, longitude = event.latitude, event.longitude
        us = "United States" in label or (
            latitude is not None and longitude is not None and (
                24 <= latitude <= 50 and -125 <= longitude <= -66 or
                51 <= latitude <= 72 and -170 <= longitude <= -129 or
                18 <= latitude <= 23 and -161 <= longitude <= -154
            )
        )
        if not us:
            continue
        if tier == "PRO":
            state = str(profile.get("state", "")).strip()
            topics = profile.get("topics", [])
            if not state or not isinstance(topics, list) or not topics:
                raise ValueError("professional profile needs a state and topics")
            fields = {f.field: str(f.value).upper() for f in repo.current_facts(conn, event.event_id)}
            if state.upper() != fields.get("STATE") and _state_name(state).lower() not in label.lower():
                continue
            if event.event_type not in topics:
                continue
            if event.impact_score < float(profile.get("impact_minimum", 0)):
                continue
        sources = [s.split(":", 1)[0] for s in event.source_ids]
        if not sources or repo.has_restricted_source(conn, event.event_id):
            continue
        rights = {}
        for source in sources:
            row = conn.execute(
                "SELECT * FROM source_rights_decisions WHERE source=?", (source,)
            ).fetchone()
            if not row or not all(row[k] for k in (
                "ingest_allowed", "summary_allowed", "commercial_publication_allowed"
            )) or row["expires_at"] and _date(row["expires_at"]) <= now:
                break
            rights[source] = row
        if len(rights) != len(sources):
            continue
        all_facts = repo.current_facts(conn, event.event_id)
        expiry = next((f.value for f in all_facts if f.field == "expires"), None)
        if expiry and _date(str(expiry)) <= now:
            continue
        facts = [f for f in all_facts if f.field in _FIELDS]
        evidence = []
        for fact in facts:
            row = conn.execute(
                "SELECT feed_url, raw_payload, source_updated_at FROM observations WHERE observation_id=?",
                (fact.observation_id,),
            ).fetchone()
            host = urlsplit(rights[fact.source]["terms_url"]).hostname if fact.source in rights else None
            if not row or not host or not _url(row["feed_url"] or "", host):
                continue
            url = _source_link(row["raw_payload"], row["feed_url"], host)
            evidence.append({
                "fact_id": fact.fact_id, "field": fact.field,
                "value": fact.value_display, "source": fact.source,
                "url": url, "source_date": row["source_updated_at"],
                "rights_reviewed_at": rights[fact.source]["reviewed_at"],
            })
        if not evidence:
            continue
        selected.append({
            "event_id": event.event_id, "revision": event.current_revision,
            "title": event.title or event.event_type, "impact": event.impact_level,
            "evidence": evidence,
        })
    return selected[:10]


def _render(tier: str, events: list[dict], note: str = "") -> tuple[str, str]:
    lines = [
        "JARVINTELS — independent public-source intelligence analysis",
        f"{tier} briefing | Not a government agency or emergency alert service.",
        "For urgent instructions, consult the linked official sources.",
    ]
    if note:
        lines.append(f"EDITORIAL NOTE (INFERENCE — REVIEW REQUIRED): {note}")
    for event in events:
        lines.append(f"\n{event['title']} | {event['impact']}")
        for fact in event["evidence"]:
            lines.append(
                f"OBSERVED FACT ({fact['source']}): {fact['field'].replace('_', ' ')}: "
                f"{fact['value']} [{fact['fact_id']}] {fact['url']}"
            )
        if tier == "PRO":
            lines.append("ANALYST WORKFLOW: Check cited source chronology and changes before operational use.")
        lines.append("UNKNOWN: No independent assessment of downstream effects is supplied.")
    text = "\n".join(lines)
    html_body = "<html><body>" + "<br>\n".join(html.escape(line) for line in lines) + "</body></html>"
    return text, html_body


def _hash(text: str, html_body: str, evidence: list, refs: list) -> str:
    return hashlib.sha256(json.dumps(
        [text, html_body, evidence, refs], sort_keys=True, ensure_ascii=False
    ).encode()).hexdigest()


def _write_revision(conn: sqlite3.Connection, settings: Settings, draft_id: str,
                    tier: str, events: list[dict], revision_id: int, note: str = "") -> None:
    evidence = [fact for event in events for fact in event["evidence"]]
    refs = [{"event_id": e["event_id"], "revision": e["revision"]} for e in events]
    text, html_body = _render(tier, events, note)
    digest = _hash(text, html_body, evidence, refs)
    now = repo.utcnow()
    conn.execute(
        "INSERT INTO briefing_revisions VALUES (?,?,?,?,?,?,?,?,?)",
        (draft_id, revision_id, text, html_body, json.dumps(evidence), json.dumps(refs),
         digest, json.dumps({"ok": bool(evidence), "review_required": True}), now),
    )
    conn.execute(
        "UPDATE briefing_drafts SET current_revision=?, status='READY_FOR_REVIEW' WHERE draft_id=?",
        (revision_id, draft_id),
    )
    recipient = settings.get("editorial.to", "jarvintels@gmail.com")
    previous = conn.execute(
        "SELECT message_id FROM editorial_outbox WHERE draft_id=? ORDER BY revision_id DESC LIMIT 1",
        (draft_id,),
    ).fetchone()
    key = f"{draft_id}:{revision_id}:{recipient}:review"
    conn.execute(
        "INSERT INTO editorial_outbox (idempotency_key, draft_id, revision_id, recipient, "
        "kind, next_attempt_at, message_id, in_reply_to, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (key, draft_id, revision_id, recipient, "review", now,
         f"<{repo.new_id('review')}@jarvintels.invalid>",
         previous["message_id"] if previous else None, now),
    )


def compose(conn: sqlite3.Connection, settings: Settings, tier: str,
            profile: dict | None = None) -> str | None:
    if tier not in ("FREE", "PRO"):
        raise ValueError("unsupported tier")
    profile = profile or {}
    events = _eligible(conn, settings, tier, profile)
    if not events:
        return None
    scope = json.dumps([tier, profile, [(e["event_id"], e["revision"]) for e in events]], sort_keys=True)
    draft_id = "brief_" + hashlib.sha256(scope.encode()).hexdigest()[:20]
    with tx(conn):
        if conn.execute("SELECT 1 FROM briefing_drafts WHERE draft_id=?", (draft_id,)).fetchone():
            return draft_id
        conn.execute(
            "INSERT INTO briefing_drafts VALUES (?,?,?,?,?,?)",
            (draft_id, tier, json.dumps(profile, sort_keys=True), "COMPOSING", 0, repo.utcnow()),
        )
        _write_revision(conn, settings, draft_id, tier, events, 1)
    return draft_id


def revise(conn: sqlite3.Connection, settings: Settings, draft_id: str, note: str) -> int:
    if not note.strip():
        raise ValueError("revision note required")
    with tx(conn):
        draft = conn.execute("SELECT * FROM briefing_drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if not draft or draft["status"] != "CHANGES_REQUESTED":
            raise ValueError("draft not awaiting changes")
        events = _eligible(conn, settings, draft["tier"], json.loads(draft["profile_json"]))
        if not events:
            raise ValueError("no eligible evidence for revision")
        revision = draft["current_revision"] + 1
        _write_revision(conn, settings, draft_id, draft["tier"], events, revision, note.strip())
    return revision


def reconcile(conn: sqlite3.Connection, settings: Settings | None = None) -> int:
    settings = settings or load_settings()
    now = datetime.now(timezone.utc)
    count = 0
    with tx(conn):
        rows = conn.execute(
            "SELECT d.draft_id, r.event_refs_json, r.evidence_json, r.created_at "
            "FROM briefing_drafts d "
            "JOIN briefing_revisions r ON r.draft_id=d.draft_id AND r.revision_id=d.current_revision "
            "WHERE d.status IN ('READY_FOR_REVIEW','APPROVED')"
        ).fetchall()
        for row in rows:
            stale = now - _date(row["created_at"]) > timedelta(
                hours=int(settings.get("editorial.review_window_hours", 24))
            )
            for ref in json.loads(row["event_refs_json"]):
                event = repo.get_event(conn, ref["event_id"])
                if not event or event.current_revision != ref["revision"] or (
                    now - _date(event.last_updated) > timedelta(
                        hours=int(settings.get("editorial.max_age_hours", 48))
                    )
                ):
                    stale = True
            for evidence in json.loads(row["evidence_json"]):
                rights = conn.execute(
                    "SELECT * FROM source_rights_decisions WHERE source=?", (evidence["source"],)
                ).fetchone()
                if not rights or not rights["commercial_publication_allowed"] or (
                    rights["expires_at"] and _date(rights["expires_at"]) <= now
                ):
                    stale = True
            if stale:
                conn.execute("UPDATE briefing_drafts SET status='EXPIRED' WHERE draft_id=?",
                             (row["draft_id"],))
                count += 1
    return count
