"""Alert and digest helpers for the outcome-intelligence loop."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import LOG_DIR
from applypilot.outcome_operator import build_action_queue, build_operator_payload


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_alerts(conn, *, now_iso: str | None = None) -> list[dict]:
    now_iso = now_iso or _now_iso()
    action_queue = build_action_queue(conn, now_iso=now_iso)
    alerts = []
    for row in action_queue:
        alerts.append({
            "message_id": row["message_id"],
            "thread_id": row["thread_id"],
            "job_url": row["job_url"],
            "company": row["company"],
            "title": row["title"],
            "stage": row["stage"],
            "outcome": row["outcome"],
            "severity": row["severity"],
            "occurred_at": row["occurred_at"],
            "subject": row["subject"],
            "reason": row["reason"],
        })
    alerts.sort(
        key=lambda row: (
            0 if row["severity"] == "critical" else 1,
            row.get("occurred_at") or "",
        ),
        reverse=False,
    )
    return alerts


def _digest_text(payload: dict, alerts: list[dict]) -> str:
    lines = [
        "ApplyPilot outcome digest",
        "",
        f"applications: {payload['summary']['applications']}",
        f"review_queue: {payload['summary']['review_queue']}",
        f"action_queue: {payload['summary']['action_queue']}",
        f"critical_alerts: {sum(1 for alert in alerts if alert['severity'] == 'critical')}",
        f"warning_alerts: {sum(1 for alert in alerts if alert['severity'] == 'warning')}",
        "",
    ]
    for alert in alerts:
        lines.append(
            f"[{alert['severity']}] {alert.get('company') or '?'} | {alert.get('stage') or '?'} | "
            f"{alert.get('subject') or ''}"
        )
    return "\n".join(lines) + "\n"


def write_digest(conn, *, output_dir: str | Path | None = None, now_iso: str | None = None) -> dict:
    now_iso = now_iso or _now_iso()
    payload = build_operator_payload(conn, now_iso=now_iso)
    alerts = build_alerts(conn, now_iso=now_iso)
    destination = Path(output_dir) if output_dir else LOG_DIR / "outcome-digest"
    destination.mkdir(parents=True, exist_ok=True)
    text = _digest_text(payload, alerts)
    summary = {
        "generated_at": now_iso,
        "applications": payload["summary"]["applications"],
        "review_queue_count": payload["summary"]["review_queue"],
        "action_queue_count": payload["summary"]["action_queue"],
        "critical_count": sum(1 for alert in alerts if alert["severity"] == "critical"),
        "warning_count": sum(1 for alert in alerts if alert["severity"] == "warning"),
        "alerts": alerts,
    }
    (destination / "outcome_digest.txt").write_text(text, encoding="utf-8")
    (destination / "outcome_digest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    summary["digest_dir"] = str(destination)
    return summary
