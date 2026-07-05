"""Read-only fleet repair report.

This module answers the operator question "what can safely be repaired?" without
changing fleet state. Mutation stays in narrowly scoped commands such as
email_reconcile and dedup_repair.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any

from applypilot.fleet import email_reconcile, queue_diagnosis


def classify_crash_error(apply_error: str | None) -> str:
    token = (apply_error or "").strip().lower()
    if "no_result_line" in token:
        return "no_result_line"
    if "timeout" in token:
        return "timeout"
    if token.startswith("failed:worker_error"):
        return "worker_error"
    if token == "crash_unconfirmed":
        return "watchdog_reclaim"
    return "unknown"


def _last_result_line(*texts: str | None) -> str | None:
    for text in texts:
        if not text:
            continue
        for line in reversed(str(text).splitlines()):
            stripped = line.strip()
            if "RESULT:" in stripped:
                return stripped[:500]
    return None


def _status_counts(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT status::text AS status, COUNT(*) AS n FROM apply_queue GROUP BY status")
        return {str(r["status"]): int(r["n"] or 0) for r in cur.fetchall()}


def _crash_summary(conn, *, sample_limit: int = 5) -> dict[str, Any]:
    limit = max(int(sample_limit), 1)
    buckets: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              q.url,
              q.application_url,
              q.company,
              q.title,
              COALESCE(q.target_host, q.apply_domain) AS host,
              q.apply_error,
              q.worker_id,
              q.agent_model,
              q.apply_duration_ms,
              q.est_cost_usd,
              q.updated_at,
              wh.machine_owner,
              wh.home_ip,
              wh.recent_log,
              wh.last_error
            FROM apply_queue q
            LEFT JOIN worker_heartbeat wh ON wh.worker_id = q.worker_id
            WHERE q.status='crash_unconfirmed'
            ORDER BY q.updated_at DESC, q.url
            """
        )
        rows = cur.fetchall()

    for row in rows:
        bucket = classify_crash_error(row["apply_error"])
        entry = buckets.setdefault(
            bucket,
            {
                "count": 0,
                "samples": [],
                "recommended_action": _bucket_action(bucket),
            },
        )
        entry["count"] += 1
        if len(entry["samples"]) >= limit:
            continue
        updated_at = row["updated_at"]
        entry["samples"].append(
            {
                "url": row["url"],
                "application_url": row["application_url"],
                "company": row["company"],
                "title": row["title"],
                "host": row["host"],
                "apply_error": row["apply_error"],
                "worker_id": row["worker_id"],
                "machine_owner": row["machine_owner"],
                "home_ip": row["home_ip"],
                "agent_model": row["agent_model"],
                "apply_duration_ms": row["apply_duration_ms"],
                "est_cost_usd": float(row["est_cost_usd"] or 0),
                "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
                "last_result_line": _last_result_line(row["recent_log"], row["last_error"]),
            }
        )

    return {"total": len(rows), "buckets": buckets}


def _bucket_action(bucket: str) -> str:
    return {
        "no_result_line": "check email_reconcile before any requeue; the form may have submitted",
        "timeout": "inspect worker/model/host samples; do not auto-requeue without evidence",
        "watchdog_reclaim": "inspect worker heartbeat/logs; row was reclaimed after a lost lease",
        "worker_error": "diagnose worker exception before retrying",
        "unknown": "review manually",
    }.get(bucket, "review manually")


def _email_summary(conn, *, home_db_path: str | None, min_score: float) -> dict[str, Any]:
    if not home_db_path:
        return {
            "jobs_total": 0,
            "confirmed": 0,
            "probable": 0,
            "unmatched_emails": 0,
            "confirmed_methods": {},
            "probable_methods": {},
            "error": "home_db_not_configured",
        }
    try:
        home = sqlite3.connect(f"file:{home_db_path}?mode=ro", uri=True)
        try:
            emails = email_reconcile.load_outcome_emails(home)
        finally:
            home.close()
    except sqlite3.OperationalError as exc:
        return {
            "jobs_total": 0,
            "confirmed": 0,
            "probable": 0,
            "unmatched_emails": 0,
            "confirmed_methods": {},
            "probable_methods": {},
            "error": f"cannot_open_home_db: {exc}",
        }

    jobs = email_reconcile.load_crash_jobs(conn)
    result = email_reconcile.reconcile(emails, jobs, min_strong=min_score)
    confirmed_methods = Counter(r.method for r in result.confirmed)
    probable_methods = Counter(r.method for r in result.probable)
    return {
        "jobs_total": result.jobs_total,
        "confirmed": len(result.confirmed),
        "probable": len(result.probable),
        "unmatched_emails": result.unmatched_emails,
        "confirmed_methods": dict(sorted(confirmed_methods.items())),
        "probable_methods": dict(sorted(probable_methods.items())),
        "confirmed_samples": [_resolution_dict(r) for r in result.confirmed[:10]],
        "probable_samples": [_resolution_dict(r) for r in result.probable[:10]],
    }


def _resolution_dict(res) -> dict[str, Any]:
    return {
        "job_url": res.job_url,
        "message_id": res.message_id,
        "method": res.method,
        "score": res.score,
        "stage": res.stage,
        "occurred_at": res.occurred_at,
        "classification": res.classification,
    }


def _recommendations(report: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    email = report.get("email_reconcile", {})
    confirmed = int(email.get("confirmed") or 0)
    if confirmed:
        limit = max(int(email.get("jobs_total") or confirmed), confirmed)
        out.append(
            {
                "priority": "safe",
                "reason": f"{confirmed} crash_unconfirmed job(s) have strong outcome-email evidence.",
                "command": (
                    "applypilot-fleet-reconcile-email --no-scan --limit "
                    f"{limit} --apply --confirmed-only --max-flips {confirmed}"
                ),
            }
        )
    probable = int(email.get("probable") or 0)
    if probable:
        out.append(
            {
                "priority": "review",
                "reason": f"{probable} probable email match(es) need manual review before flipping.",
                "command": "applypilot-fleet-reconcile-email --no-scan --json",
            }
        )

    qd = report.get("queue_diagnosis", {})
    queued = qd.get("queued", {})
    overbroad = qd.get("dedup", {}).get("overbroad_groups", [])
    if overbroad:
        group = overbroad[0]
        out.append(
            {
                "priority": "dry-run",
                "reason": (
                    f"{queued.get('dedup_blocked', 0)} approved queued row(s) are dedup-blocked; "
                    "start with the largest overbroad dedup group."
                ),
                "command": f"applypilot-fleet-dedup-repair --dedup-key {group['dedup_key']} --json",
            }
        )
    if int(queued.get("total") or 0) and not int(queued.get("base_leaseable") or 0):
        out.append(
            {
                "priority": "info",
                "reason": "Raw queued rows exist, but none are currently leaseable after approval/dedup gates.",
                "command": "applypilot-fleet-apply-home status",
            }
        )
    return out


def build_report(
    conn,
    *,
    home_db_path: str | None = None,
    min_score: float = email_reconcile.MIN_STRONG,
    sample_limit: int = 5,
    overbroad_limit: int = 10,
) -> dict[str, Any]:
    report = {
        "queue": {"status_counts": _status_counts(conn)},
        "crash": _crash_summary(conn, sample_limit=sample_limit),
        "email_reconcile": _email_summary(conn, home_db_path=home_db_path, min_score=min_score),
        "queue_diagnosis": queue_diagnosis.queue_diagnosis(conn, overbroad_limit=overbroad_limit),
    }
    report["recommendations"] = _recommendations(report)
    return report


def format_text(report: dict[str, Any]) -> str:
    lines = ["fleet repair report"]
    lines.append(f"queue: {report.get('queue', {}).get('status_counts', {})}")
    crash = report.get("crash", {})
    lines.append(f"crash_unconfirmed: {crash.get('total', 0)}")
    for bucket, data in sorted(crash.get("buckets", {}).items()):
        lines.append(f"  {bucket}: {data.get('count', 0)} - {data.get('recommended_action', '')}")
    email = report.get("email_reconcile", {})
    lines.append(
        "email reconcile: "
        f"confirmed={email.get('confirmed', 0)} probable={email.get('probable', 0)} "
        f"jobs={email.get('jobs_total', 0)}"
    )
    qd = report.get("queue_diagnosis", {}).get("queued", {})
    lines.append(
        "queued diagnosis: "
        f"total={qd.get('total', 0)} approved_ats={qd.get('approved_ats', 0)} "
        f"dedup_blocked={qd.get('dedup_blocked', 0)} base_leaseable={qd.get('base_leaseable', 0)}"
    )
    recs = report.get("recommendations", [])
    if recs:
        lines.append("recommendations:")
        for rec in recs:
            lines.append(f"  [{rec.get('priority')}] {rec.get('reason')} :: {rec.get('command')}")
    return "\n".join(lines)
