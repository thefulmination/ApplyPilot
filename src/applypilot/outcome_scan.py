"""Scan Gmail for application-outcome emails and persist them to email_events.

Reuses the read-only Gmail OAuth, job-matching, and email-parsing already in
gmail_outcomes; adds LLM extraction (outcome_extract) and an idempotent upsert.
The Gmail fetch is injectable (`fetch_messages`) so the pipeline is testable
without the network."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from applypilot.database import get_connection
from applypilot.gmail_outcomes import (
    _extract_domain,
    get_applied_jobs,
    match_email_to_job,
)
from applypilot.outcome_extract import extract_outcome


def _occurred_at(date_header: str) -> str:
    """Parse an RFC-2822 Date header into ISO-8601 (UTC). Falls back to now."""
    try:
        dt = parsedate_to_datetime(date_header)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def build_email_event(msg: dict, applied_jobs: list[dict], *, client=None) -> dict:
    """Turn one fetched message into a full email_events row dict."""
    subject = msg.get("subject", "") or ""
    sender = msg.get("sender", "") or ""
    body = msg.get("body", "") or ""

    ex = extract_outcome(subject, body, sender, client=client)
    job, method, score = match_email_to_job(sender, subject, body, applied_jobs)

    return {
        "message_id": msg["message_id"],
        "thread_id": msg.get("thread_id"),
        "job_url": job.get("url") if job else None,
        "occurred_at": _occurred_at(msg.get("date", "")),
        "sender": sender,
        "sender_domain": _extract_domain(sender),
        "subject": subject,
        "stage": ex.stage,
        "outcome": ex.outcome,
        "reason": ex.reason,
        "title": ex.title or (job.get("title") if job else None),
        "company": ex.company or (job.get("company") if job else None),
        "match_method": method,
        "match_score": score,
        "confidence": ex.confidence,
        "body_text": body[:20000],
        "snippet": body[:300].replace("\n", " ").strip(),
        "extracted_by": ex.extracted_by,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


_COLUMNS = (
    "message_id", "thread_id", "job_url", "occurred_at", "sender", "sender_domain",
    "subject", "stage", "outcome", "reason", "title", "company", "match_method",
    "match_score", "confidence", "body_text", "snippet", "extracted_by", "scanned_at",
)


def upsert_email_event(conn, row: dict, *, reextract: bool = False) -> str:
    """Insert a new email_events row. If the message_id exists, skip (default)
    or overwrite (reextract=True). Returns 'inserted' | 'skipped' | 'updated'."""
    existing = conn.execute(
        "SELECT 1 FROM email_events WHERE message_id = ?", (row["message_id"],)
    ).fetchone()
    if existing and not reextract:
        return "skipped"

    values = [row.get(c) for c in _COLUMNS]
    if existing:
        assignments = ", ".join(f"{c} = ?" for c in _COLUMNS if c != "message_id")
        conn.execute(
            f"UPDATE email_events SET {assignments} WHERE message_id = ?",
            [row.get(c) for c in _COLUMNS if c != "message_id"] + [row["message_id"]],
        )
        conn.commit()
        return "updated"

    placeholders = ", ".join("?" for _ in _COLUMNS)
    conn.execute(
        f"INSERT INTO email_events ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return "inserted"


def _gmail_fetch(days: int, credentials_path: Path | None) -> Callable[[], list[dict]]:
    """Default fetch: pull candidate messages from Gmail (read-only). Returns a
    thunk so the network call is deferred until scan time."""
    def _fetch() -> list[dict]:
        from applypilot.gmail_outcomes import build_gmail_service, _get_text_body, _search_query
        service = build_gmail_service(credentials_path=credentials_path)
        query = _search_query(days)
        resp = service.users().messages().list(userId="me", q=query, maxResults=200).execute()
        out: list[dict] = []
        seen: set[str] = set()
        for ref in resp.get("messages", []):
            tid = ref.get("threadId", ref["id"])
            if tid in seen:
                continue
            seen.add(tid)
            full = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            headers = {h["name"].lower(): h["value"] for h in full.get("payload", {}).get("headers", [])}
            out.append({
                "message_id": ref["id"],
                "thread_id": tid,
                "subject": headers.get("subject", ""),
                "sender": headers.get("from", ""),
                "date": headers.get("date", ""),
                "body": _get_text_body(full.get("payload", {})),
            })
        return out
    return _fetch


def scan_outcomes(
    days: int = 30,
    *,
    credentials_path: Path | None = None,
    client=None,
    reextract: bool = False,
    conn=None,
    fetch_messages: Callable[[], list[dict]] | None = None,
) -> dict[str, int]:
    """Scan candidate emails and upsert them into email_events. Returns counts."""
    if conn is None:
        conn = get_connection()
    fetch = fetch_messages or _gmail_fetch(days, credentials_path)

    applied_jobs = get_applied_jobs(conn)
    counts = {"inserted": 0, "skipped": 0, "updated": 0, "errors": 0}
    for msg in fetch():
        try:
            row = build_email_event(msg, applied_jobs, client=client)
            counts[upsert_email_event(conn, row, reextract=reextract)] += 1
        except Exception:
            counts["errors"] += 1
    return counts
