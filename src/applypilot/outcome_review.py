"""Outcome review storage and effective-state assembly.

`email_events` remains the raw evidence layer written by the scanner. This
module adds an append-only review layer and resolves the effective event view
consumed by CLI/dashboard/export code.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from applypilot.database import get_connection

REVIEW_RESOLUTIONS = {"trusted", "needs_review", "ignored", "corrected"}
HIGH_VALUE_STAGES = {"screen", "assessment", "interview", "offer"}
TERMINAL_STAGES = {"offer", "rejected", "position_filled"}
DEFAULT_ACTION_FOR_RESOLUTION = {
    "trusted": "confirm",
    "needs_review": "confirm",
    "ignored": "ignore",
    "corrected": "change_stage",
}
TRUSTED_STATES = {"trusted", "corrected"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _latest_reviews(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.*
          FROM email_event_reviews r
          JOIN (
                SELECT message_id, MAX(id) AS max_id
                  FROM email_event_reviews
              GROUP BY message_id
          ) latest
            ON latest.message_id = r.message_id
           AND latest.max_id = r.id
        """
    ).fetchall()
    return {row["message_id"]: _row_to_dict(row) for row in rows}


def record_review(
    conn,
    message_id: str,
    *,
    resolution: str,
    review_action: str | None = None,
    corrected_job_url: str | None = None,
    corrected_stage: str | None = None,
    corrected_outcome: str | None = None,
    corrected_confidence: str | None = None,
    reviewed_by: str | None = "cli",
    note: str | None = None,
) -> dict[str, Any]:
    if resolution not in REVIEW_RESOLUTIONS:
        raise ValueError(f"invalid resolution '{resolution}'")
    exists = conn.execute(
        "SELECT 1 FROM email_events WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    if not exists:
        raise ValueError(f"unknown message_id '{message_id}'")

    action = review_action or DEFAULT_ACTION_FOR_RESOLUTION[resolution]
    reviewed_at = _now_iso()
    cur = conn.execute(
        """
        INSERT INTO email_event_reviews (
            message_id, review_action, reviewed_by, reviewed_at,
            corrected_job_url, corrected_stage, corrected_outcome,
            corrected_confidence, resolution, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            action,
            reviewed_by,
            reviewed_at,
            corrected_job_url,
            corrected_stage,
            corrected_outcome,
            corrected_confidence,
            resolution,
            note,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM email_event_reviews WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()
    return _row_to_dict(row)


def list_reviews(conn, message_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM email_event_reviews WHERE message_id = ? ORDER BY id",
        (message_id,),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _apply_review(event: dict[str, Any], review: dict[str, Any] | None) -> dict[str, Any]:
    resolved = dict(event)
    resolved["raw_job_url"] = event.get("job_url")
    resolved["raw_stage"] = event.get("stage")
    resolved["raw_outcome"] = event.get("outcome")
    resolved["raw_confidence"] = event.get("confidence")
    resolved["review_resolution"] = review.get("resolution") if review else None
    resolved["review_action"] = review.get("review_action") if review else None
    resolved["reviewed_at"] = review.get("reviewed_at") if review else None
    resolved["note"] = review.get("note") if review else None

    if review:
        if review.get("corrected_job_url"):
            resolved["job_url"] = review["corrected_job_url"]
        if review.get("corrected_stage"):
            resolved["stage"] = review["corrected_stage"]
        if review.get("corrected_outcome") is not None:
            resolved["outcome"] = review["corrected_outcome"] or None
        elif review.get("corrected_stage") and review["corrected_stage"] not in TERMINAL_STAGES:
            resolved["outcome"] = None
        if review.get("corrected_confidence"):
            resolved["confidence"] = review["corrected_confidence"]
        trust_state = review["resolution"]
    elif event.get("match_status") == "needs_review":
        trust_state = "needs_review"
    elif event.get("job_url"):
        trust_state = "trusted"
    else:
        trust_state = "needs_review"

    resolved["trust_state"] = trust_state
    resolved["excluded"] = trust_state == "ignored"
    resolved["is_trusted"] = trust_state in TRUSTED_STATES
    resolved["needs_review"] = trust_state == "needs_review"
    resolved["high_value"] = (
        resolved.get("stage") in HIGH_VALUE_STAGES
        or resolved.get("outcome") == "offer"
    )
    resolved["actioned"] = resolved.get("review_action") == "mark_actioned"
    resolved["needs_action"] = resolved["high_value"] and not resolved["actioned"]
    return resolved


def build_effective_events(
    conn=None,
    *,
    include_ignored: bool = True,
    trusted_only: bool = False,
) -> list[dict[str, Any]]:
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        """
        SELECT message_id, thread_id, job_url, occurred_at, sender, sender_domain, subject,
               stage, outcome, reason, title, company, match_method, match_score,
               confidence, body_text, snippet, extracted_by, scanned_at,
               match_status, match_reason, prev_job_url
          FROM email_events
      ORDER BY occurred_at, message_id
        """
    ).fetchall()
    reviews = _latest_reviews(conn)
    events = []
    for row in rows:
        event = _apply_review(_row_to_dict(row), reviews.get(row["message_id"]))
        if not include_ignored and event["excluded"]:
            continue
        if trusted_only and not event["is_trusted"]:
            continue
        if include_ignored or not event["excluded"]:
            events.append(event)
    return events


def build_effective_events_for_job(conn, job_url: str, *, trusted_only: bool = False) -> list[dict[str, Any]]:
    rows = [
        row for row in build_effective_events(conn, include_ignored=False, trusted_only=trusted_only)
        if row.get("job_url") == job_url
    ]
    rows.sort(key=lambda row: (row.get("occurred_at") or "", row.get("message_id") or ""))
    return rows


def build_effective_event_counts(conn) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "trusted_event_count": 0,
            "needs_review_event_count": 0,
            "excluded_event_count": 0,
        }
    )
    for row in build_effective_events(conn, include_ignored=True):
        job_url = row.get("job_url")
        if not job_url:
            continue
        if row["excluded"]:
            counts[job_url]["excluded_event_count"] += 1
        elif row["is_trusted"]:
            counts[job_url]["trusted_event_count"] += 1
        else:
            counts[job_url]["needs_review_event_count"] += 1
    return counts


def list_review_queue(conn=None) -> list[dict[str, Any]]:
    if conn is None:
        conn = get_connection()
    rows = [row for row in build_effective_events(conn, include_ignored=False) if row["needs_review"]]
    rows.sort(key=lambda row: (row.get("occurred_at") or "", row.get("message_id") or ""))
    return rows
