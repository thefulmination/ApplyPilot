"""Export email_events + per-application outcome timelines as JSONL for the
learning loop / external tools. Read-only on the brain; writes only export files.
Mirrors applications.export_outcomes' file convention exactly."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot.config import APPLICATION_EXPORT_DIR
from applypilot.outcome_dashboard import build_application_rows
from applypilot.outcome_implied import implied_status
from applypilot.outcome_lane_signal import annotate_job_signal, compute_lane_report

_RAW_EVENTS_SQL = (
    "SELECT message_id, thread_id, job_url, occurred_at, sender, sender_domain, subject, "
    "stage, outcome, reason, title, company, match_method, match_score, confidence, "
    "snippet, extracted_by, scanned_at FROM email_events ORDER BY occurred_at"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _lean_event(ev: dict[str, Any]) -> dict[str, Any]:
    """Drop body_text from a timeline event (keep snippet) to keep the file lean."""
    return {k: v for k, v in ev.items() if k != "body_text"}


def export_outcome_events(output_dir: str | Path | None = None, *, conn=None) -> dict[str, Any]:
    """Write email_events.jsonl + outcome_timelines.jsonl (+ a summary sidecar) to a
    timestamped folder under APPLICATION_EXPORT_DIR. The timelines file is enriched
    with the inert #3 `implied_status` and advisory #5 `outcome_signal` fields."""
    if conn is None:
        from applypilot.database import get_connection
        conn = get_connection()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = Path(output_dir) if output_dir else APPLICATION_EXPORT_DIR / timestamp
    destination.mkdir(parents=True, exist_ok=True)

    email_events = [_row_to_dict(r) for r in conn.execute(_RAW_EVENTS_SQL).fetchall()]
    rows = build_application_rows(conn)
    report = compute_lane_report(rows)
    timelines = []
    for row in rows:
        enriched = dict(row)
        enriched["events"] = [_lean_event(e) for e in row.get("events", [])]
        enriched["implied_status"] = implied_status(row)        # #3 inert: derived, not written
        enriched["outcome_signal"] = annotate_job_signal(row, report)  # #5 advisory
        timelines.append(enriched)

    events_path = destination / "email_events.jsonl"
    timelines_path = destination / "outcome_timelines.jsonl"
    _write_jsonl(events_path, email_events)
    _write_jsonl(timelines_path, timelines)

    summary = {
        "exported_at": _now(),
        "email_events_exported": len(email_events),
        "outcome_timelines_exported": len(timelines),
        "email_events_path": str(events_path),
        "outcome_timelines_path": str(timelines_path),
        "output_dir": str(destination),
    }
    (destination / "outcomes_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
