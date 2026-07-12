"""Pure operator payload assembly for the outcome-intelligence loop."""

from __future__ import annotations

from datetime import datetime, timezone

from applypilot.outcome_review import (
    build_effective_event_counts,
    build_effective_events,
    list_review_queue,
)
from applypilot.outcome_timeline import build_timeline
from applypilot.lane_insights import derive_segments

_UNIVERSE_SQL = """
    SELECT j.url, j.title, j.company, j.source_board, j.location, j.salary,
           j.fit_score, j.audit_score, j.fit_gap_category, j.applied_at
      FROM jobs j
      LEFT JOIN applications a ON a.job_url = j.url
     WHERE j.apply_status = 'applied' OR a.job_url IS NOT NULL
     ORDER BY COALESCE(j.applied_at, j.discovered_at) DESC
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tracked_universe(conn) -> list[dict]:
    return [dict(row) for row in conn.execute(_UNIVERSE_SQL).fetchall()]


def build_action_queue(conn, *, now_iso: str | None = None) -> list[dict]:
    now_iso = now_iso or _now_iso()
    universe = {
        row["url"]: dict(row)
        for row in _tracked_universe(conn)
    }
    latest_by_thread: dict[str, dict] = {}
    for event in build_effective_events(conn, include_ignored=False):
        if not event.get("needs_action"):
            continue
        thread_key = str(event.get("thread_id") or event.get("message_id") or "")
        existing = latest_by_thread.get(thread_key)
        if existing is None or (event.get("occurred_at") or "") >= (existing.get("occurred_at") or ""):
            latest_by_thread[thread_key] = event

    rows = []
    for event in sorted(latest_by_thread.values(), key=lambda row: (row.get("occurred_at") or "", row.get("message_id") or "")):
        job = universe.get(event.get("job_url") or "", {})
        rows.append({
            "message_id": event.get("message_id"),
            "thread_id": event.get("thread_id"),
            "job_url": event.get("job_url"),
            "company": event.get("company") or job.get("company"),
            "title": event.get("title") or job.get("title"),
            "stage": event.get("stage"),
            "outcome": event.get("outcome"),
            "trust_state": event.get("trust_state"),
            "occurred_at": event.get("occurred_at"),
            "subject": event.get("subject"),
            "reason": event.get("reason") or event.get("match_reason"),
            "severity": "critical" if event.get("is_trusted") else "warning",
        })
    return rows


def build_operator_payload(conn, *, now_iso: str | None = None) -> dict:
    now_iso = now_iso or _now_iso()
    review_queue = list_review_queue(conn)
    action_queue = build_action_queue(conn, now_iso=now_iso)
    trusted_events = build_effective_events(conn, include_ignored=False, trusted_only=True)
    evidence_events = build_effective_events(conn, include_ignored=False, trusted_only=False)
    trusted_by_job: dict[str, list[dict]] = {}
    evidence_by_job: dict[str, list[dict]] = {}
    for event in trusted_events:
        job_url = event.get("job_url")
        if job_url:
            trusted_by_job.setdefault(job_url, []).append(event)
    for event in evidence_events:
        job_url = event.get("job_url")
        if job_url:
            evidence_by_job.setdefault(job_url, []).append(event)

    counts_by_job = build_effective_event_counts(conn)
    rows = []
    for job in _tracked_universe(conn):
        job_url = job["url"]
        events = sorted(trusted_by_job.get(job_url, []), key=lambda row: row.get("occurred_at") or "")
        evidence = sorted(evidence_by_job.get(job_url, []), key=lambda row: row.get("occurred_at") or "")
        tl = build_timeline(job.get("applied_at"), events, now_iso=now_iso)
        counts = counts_by_job.get(job_url, {})
        rows.append({
            "job_url": job_url,
            "title": job.get("title"),
            "company": job.get("company"),
            "source_board": job.get("source_board"),
            "applied_at": job.get("applied_at"),
            "current_stage": tl["current_stage"],
            "outcome": tl["outcome"],
            "responded": tl["responded"],
            "positive": tl["positive"],
            "first_response_days": tl["first_response_days"],
            "decision_days": tl["decision_days"],
            "silent_days": tl["silent_days"],
            "segments": derive_segments(job),
            "events": tl["ordered"],
            "evidence_events": evidence,
            "trust_state": "needs_review" if counts.get("needs_review_event_count", 0) else "trusted",
            "needs_action": any(event.get("needs_action") for event in evidence),
            "trusted_event_count": counts.get("trusted_event_count", 0),
            "needs_review_event_count": counts.get("needs_review_event_count", 0),
            "excluded_event_count": counts.get("excluded_event_count", 0),
        })

    return {
        "rows": rows,
        "review_queue": review_queue,
        "action_queue": action_queue,
        "summary": {
            "applications": len(rows),
            "review_queue": len(review_queue),
            "action_queue": len(action_queue),
        },
    }
