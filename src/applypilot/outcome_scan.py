"""Scan Gmail for application-outcome emails and persist them to email_events.

Reuses the read-only Gmail OAuth, job-matching, and email-parsing already in
gmail_outcomes; adds LLM extraction (outcome_extract) and an idempotent upsert.
The Gmail fetch is injectable (`fetch_messages`) so the pipeline is testable
without the network."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)

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
    occurred = _occurred_at(msg.get("date", ""))
    m = match_email_to_job(sender, subject, body, applied_jobs, occurred_at=occurred)
    job, method, score = m.job, m.method, m.score

    return {
        "message_id": msg["message_id"],
        "thread_id": msg.get("thread_id"),
        "job_url": job.get("url") if job else None,
        "occurred_at": occurred,
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
        "match_status": m.status if m.job or m.status == "needs_review" else None,
        "match_reason": m.reason,
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
    "match_status", "match_reason",
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


def _gmail_fetch(
    days: int, credentials_path: Path | None, max_messages: int = 200
) -> Callable[[], list[dict]]:
    """Default fetch: pull up to `max_messages` candidate messages via the
    configured mail source (IMAP app-password when set, else the legacy
    OAuth-backed Gmail API -- see mail_source.get_mail_source()).
    Returns a thunk so the network call is deferred until scan time.

    `credentials_path` is retained for signature back-compat; it only has an
    effect on the legacy Gmail-API fallback path (ignored under IMAP)."""
    def _fetch() -> list[dict]:
        from applypilot.mail_source import get_mail_source
        msgs = get_mail_source().fetch(since_days=days, max_messages=max_messages)
        return [
            {
                "message_id": m.id,
                "thread_id": m.thread_id,
                "subject": m.subject,
                "sender": m.sender,
                "date": m.date,
                "body": m.body,
            }
            for m in msgs
        ]
    return _fetch


def scan_outcomes(
    days: int = 30,
    *,
    credentials_path: Path | None = None,
    client=None,
    reextract: bool = False,
    conn=None,
    fetch_messages: Callable[[], list[dict]] | None = None,
    max_messages: int = 200,
    concurrency: int = 8,
) -> dict[str, int]:
    """Scan candidate emails and upsert them into email_events. Returns counts.

    LLM extraction (the network-bound bottleneck) is parallelized across
    `concurrency` worker threads; the email_events upserts stay serial on this
    thread (SQLite single-writer). `max_messages` bounds the paginated fetch.
    """
    if conn is None:
        conn = get_connection()
    fetch = fetch_messages or _gmail_fetch(days, credentials_path, max_messages)

    applied_jobs = get_applied_jobs(conn)
    counts = {"inserted": 0, "skipped": 0, "updated": 0, "errors": 0, "needs_review": 0}
    needs_review_reasons = {"predates_application": 0, "ambiguous_company": 0, "no_timestamp": 0}
    messages = list(fetch())

    def _upsert(row: dict) -> None:
        if row.get("match_status") == "needs_review":
            counts["needs_review"] += 1
            reason = row.get("match_reason")
            if reason in needs_review_reasons:
                needs_review_reasons[reason] += 1
        counts[upsert_email_event(conn, row, reextract=reextract)] += 1

    if concurrency and concurrency > 1 and len(messages) > 1:
        # Build rows (extract + match) in parallel; upsert serially on this thread.
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(build_email_event, msg, applied_jobs, client=client): msg
                for msg in messages
            }
            for fut in as_completed(futures):
                msg = futures[fut]
                try:
                    row = fut.result()
                except Exception:
                    log.exception("outcome scan: failed to process message %s", msg.get("message_id"))
                    counts["errors"] += 1
                    continue
                try:
                    _upsert(row)
                except Exception:
                    log.exception("outcome scan: failed to upsert %s", row.get("message_id"))
                    counts["errors"] += 1
    else:
        for msg in messages:
            try:
                _upsert(build_email_event(msg, applied_jobs, client=client))
            except Exception:
                log.exception("outcome scan: failed to process message %s", msg.get("message_id"))
                counts["errors"] += 1
    counts["needs_review_reasons"] = needs_review_reasons
    return counts
