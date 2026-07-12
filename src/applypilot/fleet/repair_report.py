"""Read-only fleet repair report.

This module answers the operator question "what can safely be repaired?" without
changing fleet state. Mutation stays in narrowly scoped commands such as
email_reconcile and dedup_repair.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any

from applypilot.fleet import email_reconcile, failure_taxonomy, queue_diagnosis


def classify_crash_error(apply_error: str | None, *, event_source: str | None = None) -> str:
    if event_source == "worker_lease":
        return "lease_started_no_terminal"
    token = (apply_error or "").strip().lower()
    if "no_result_line" in token:
        return "no_result_line"
    if "timeout" in token:
        return "timeout"
    if "email_reconcile_review_required" in token:
        return "email_review_required"
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


def classify_terminal_reason(status: str, reason: str | None) -> str:
    """Put terminal rows into an operator action bucket without implying a retry is safe."""
    token = (reason or "").strip().lower()
    if any(x in token for x in ("submission_uncertain", "crash_unconfirmed", "no_confirmation")):
        return "protected_manual_review"
    if any(x in token for x in ("challenge", "auth", "login", "email_verification", "availability_unavailable")):
        return "auth_or_availability"
    if any(x in token for x in ("browser", "page_error", "stuck", "timeout", "connection_lost", "backend")):
        return "infrastructure_review"
    if any(x in token for x in ("stale_unapproved", "expired", "dedup", "already_applied", "not_eligible", "blocklist", "unsupported", "excluded")):
        return "terminal_non_retryable"
    if status == "blocked":
        return "protected_manual_review"
    return "other_review"


def _terminal_breakdown(conn) -> dict[str, Any]:
    """Summarize failed/blocked rows by safe next-action category."""
    buckets: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status::text AS status,
                   COALESCE(NULLIF(apply_error, ''), '(none)') AS reason,
                   COUNT(*) AS n,
                   MIN(updated_at) AS oldest,
                   MAX(updated_at) AS newest
              FROM apply_queue
             WHERE status IN ('failed', 'blocked')
             GROUP BY status, reason
             ORDER BY n DESC
            """
        )
        rows = cur.fetchall()
    for row in rows:
        bucket = classify_terminal_reason(row["status"], row["reason"])
        data = buckets.setdefault(bucket, {"count": 0, "reasons": []})
        data["count"] += int(row["n"] or 0)
        data["reasons"].append(
            {
                "status": row["status"],
                "reason": row["reason"],
                "count": int(row["n"] or 0),
                "oldest": row["oldest"].isoformat() if hasattr(row["oldest"], "isoformat") else row["oldest"],
                "newest": row["newest"].isoformat() if hasattr(row["newest"], "isoformat") else row["newest"],
            }
        )
    for data in buckets.values():
        data["reasons"] = data["reasons"][:10]
    return buckets


def _canonical_failure_counts(conn) -> dict[str, int]:
    """Aggregate raw terminal reasons using the shared operator taxonomy."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(NULLIF(apply_error, ''), '(none)') AS reason, COUNT(*) AS n
              FROM apply_queue
             WHERE status IN ('failed', 'blocked')
             GROUP BY reason
            """
        )
        raw = {str(row["reason"]): int(row["n"] or 0) for row in cur.fetchall()}
    return failure_taxonomy.group_reason_counts(raw)


def _infrastructure_concentration(conn, *, limit: int = 10) -> dict[str, list[dict[str, Any]]]:
    """Show where infrastructure failures cluster so remediation can target the cause."""
    categories = ("browser", "timeout", "backend", "connection_lost", "page_error", "stuck")
    pattern = " OR ".join("apply_error ILIKE %s" for _ in categories)
    params = tuple(f"%{value}%" for value in categories)
    result: dict[str, list[dict[str, Any]]] = {"hosts": [], "workers": []}
    with conn.cursor() as cur:
        for key, expression in (
            ("hosts", "COALESCE(target_host, apply_domain, '(unknown)')"),
            ("workers", "COALESCE(worker_id, '(none)')"),
        ):
            cur.execute(
                f"""
                SELECT {expression} AS name, COUNT(*) AS n
                  FROM apply_queue
                 WHERE status='failed' AND ({pattern})
                 GROUP BY {expression}
                 ORDER BY n DESC, name
                 LIMIT %s
                """,
                (*params, limit),
            )
            result[key] = [{"name": row["name"], "count": int(row["n"] or 0)} for row in cur.fetchall()]
    return result


def _worker_health(conn) -> list[dict[str, Any]]:
    """Report heartbeat liveness alongside historical infrastructure failures."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT h.worker_id, h.state, h.last_beat, h.last_error,
                   COUNT(q.url) FILTER (WHERE q.status='failed' AND (
                     q.apply_error ILIKE '%%browser%%' OR q.apply_error ILIKE '%%playwright%%'
                     OR q.apply_error ILIKE '%%backend%%' OR q.apply_error ILIKE '%%connection_lost%%'
                   )) AS infrastructure_failures,
                   EXTRACT(EPOCH FROM (now() - h.last_beat)) AS age_seconds
              FROM worker_heartbeat h
              LEFT JOIN apply_queue q ON q.worker_id = h.worker_id
             WHERE h.role='apply'
             GROUP BY h.worker_id, h.state, h.last_beat, h.last_error
             ORDER BY h.worker_id
            """
        )
        return [
            {
                "worker_id": row["worker_id"],
                "state": row["state"],
                "last_beat": row["last_beat"].isoformat() if hasattr(row["last_beat"], "isoformat") else row["last_beat"],
                "age_seconds": float(row["age_seconds"] or 0),
                "alive": float(row["age_seconds"] or 10**9) <= 150,
                "infrastructure_failures": int(row["infrastructure_failures"] or 0),
                "last_error": row["last_error"],
            }
            for row in cur.fetchall()
        ]


def _dedup_protection_summary(conn) -> dict[str, int]:
    """Measure metadata quality for rows blocked by the applied-set guard."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE a.company IS NULL OR a.applied_url IS NULL) AS weak_metadata,
                   COUNT(*) FILTER (WHERE a.company IS NOT NULL AND a.applied_url IS NOT NULL) AS strong_metadata,
                   COUNT(*) FILTER (WHERE q.dedup_key IS NULL) AS missing_dedup_key
              FROM apply_queue q
              JOIN applied_set a ON a.dedup_key=q.dedup_key
             WHERE q.status='blocked' AND q.apply_error='submission_uncertain:applied_set_protected'
            """
        )
        row = cur.fetchone()
        cur.execute(
            """
            WITH weak AS (
                SELECT a.dedup_key
                  FROM applied_set a
                  JOIN apply_queue protected
                    ON protected.dedup_key=a.dedup_key
                   AND protected.status='blocked'
                   AND protected.apply_error='submission_uncertain:applied_set_protected'
                 WHERE a.company IS NULL OR a.applied_url IS NULL
            ), evidence AS (
                SELECT w.dedup_key,
                       COUNT(q.url) rows,
                       COUNT(DISTINCT NULLIF(q.company,'')) companies,
                       COUNT(DISTINCT NULLIF(COALESCE(q.application_url,q.url),'')) urls
                  FROM weak w LEFT JOIN apply_queue q ON q.dedup_key=w.dedup_key
                 GROUP BY w.dedup_key
            )
            SELECT COUNT(*) FILTER (WHERE rows=0) AS no_queue_evidence,
                   COUNT(*) FILTER (WHERE rows>0 AND (companies>1 OR urls>1)) AS conflicting_evidence
              FROM evidence
            """
        )
        ambiguity = cur.fetchone()
    return {
        "total": int(row["total"] or 0),
        "weak_metadata": int(row["weak_metadata"] or 0),
        "strong_metadata": int(row["strong_metadata"] or 0),
        "missing_dedup_key": int(row["missing_dedup_key"] or 0),
        "no_queue_evidence": int(ambiguity["no_queue_evidence"] or 0),
        "conflicting_evidence": int(ambiguity["conflicting_evidence"] or 0),
    }


def _infrastructure_evidence_summary(conn) -> dict[str, int]:
    """Count infrastructure failures by durable execution evidence quality."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT CASE WHEN e.event_id IS NULL THEN 'no_event'
                        WHEN e.application_tool_calls IS NULL THEN 'missing_tool_calls'
                        WHEN e.application_tool_calls=0 THEN 'zero_tool_calls'
                        ELSE 'tool_touched' END AS evidence,
                   COUNT(*) AS n
              FROM apply_queue q
              LEFT JOIN LATERAL (
                SELECT id AS event_id, application_tool_calls
                  FROM apply_result_events e
                 WHERE e.queue_name='apply_queue' AND e.url=q.url
                 ORDER BY e.created_at DESC, e.id DESC
                 LIMIT 1
              ) e ON TRUE
             WHERE q.status='failed' AND (
                   q.apply_error ILIKE '%%browser%%'
                OR q.apply_error ILIKE '%%playwright%%'
                OR q.apply_error ILIKE '%%backend%%'
                OR q.apply_error ILIKE '%%connection_lost%%'
                OR q.apply_error ILIKE '%%page_error%%')
             GROUP BY evidence
            """
        )
        result = {"no_event": 0, "missing_tool_calls": 0, "zero_tool_calls": 0, "tool_touched": 0}
        for row in cur.fetchall():
            result[str(row["evidence"])] = int(row["n"] or 0)
    return result


def _recent_result_event_summary(conn, *, hours: int = 24) -> list[dict[str, Any]]:
    """Separate worker outcome events from operator repair events."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(source,'(none)') AS source, status::text AS status, COUNT(*) AS n
              FROM apply_result_events
             WHERE created_at >= now() - make_interval(hours => %s)
             GROUP BY source, status
             ORDER BY n DESC, source, status
            """,
            (hours,),
        )
        return [{"source": row["source"], "status": row["status"], "count": int(row["n"] or 0)}
                for row in cur.fetchall()]


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
              ar.result_line,
              ar.event_source,
              CASE WHEN wh.current_job = q.url THEN wh.recent_log ELSE NULL END AS recent_log,
              CASE WHEN wh.current_job = q.url THEN wh.last_error ELSE NULL END AS last_error
            FROM apply_queue q
            LEFT JOIN worker_heartbeat wh ON wh.worker_id = q.worker_id
            LEFT JOIN LATERAL (
              SELECT result_line, source AS event_source
              FROM apply_result_events e
              WHERE e.queue_name = 'apply_queue' AND e.url = q.url
              ORDER BY e.created_at DESC, e.id DESC
              LIMIT 1
            ) ar ON TRUE
            WHERE q.status='crash_unconfirmed'
            ORDER BY q.updated_at DESC, q.url
            """
        )
        rows = cur.fetchall()

    for row in rows:
        bucket = classify_crash_error(row["apply_error"], event_source=row["event_source"])
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
                "last_result_line": _last_result_line(row["result_line"], row["recent_log"], row["last_error"]),
                "event_source": row["event_source"],
            }
        )

    return {"total": len(rows), "buckets": buckets}


def _bucket_action(bucket: str) -> str:
    return {
        "no_result_line": "check email_reconcile before any requeue; the form may have submitted",
        "timeout": "inspect worker/model/host samples; do not auto-requeue without evidence",
        "email_review_required": "review prior email reconcile match before marking applied",
        "watchdog_reclaim": "inspect worker heartbeat/logs; row was reclaimed after a lost lease",
        "lease_started_no_terminal": "inspect the worker heartbeat/logs; the worker leased the job but wrote no terminal result",
        "worker_error": "diagnose worker exception before retrying",
        "unknown": "review manually",
    }.get(bucket, "review manually")


def _email_summary(
    conn,
    *,
    home_db_path: str | None,
    min_score: float,
    sample_limit: int = 5,
) -> dict[str, Any]:
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
    consumed_message_ids = email_reconcile.load_consumed_message_ids(conn)
    result = email_reconcile.reconcile(
        emails,
        jobs,
        min_strong=min_score,
        consumed_message_ids=consumed_message_ids,
    )
    confirmed_methods = Counter(r.method for r in result.confirmed)
    probable_methods = Counter(r.method for r in result.probable)
    limit = max(int(sample_limit), 1)
    return {
        "jobs_total": result.jobs_total,
        "confirmed": len(result.confirmed),
        "probable": len(result.probable),
        "unmatched_emails": result.unmatched_emails,
        "consumed_message_ids": len(consumed_message_ids),
        "confirmed_methods": dict(sorted(confirmed_methods.items())),
        "probable_methods": dict(sorted(probable_methods.items())),
        "confirmed_samples": [_resolution_dict(r) for r in result.confirmed[:limit]],
        "probable_samples": [_resolution_dict(r) for r in result.probable[:limit]],
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
        "job_company": res.job_company,
        "job_title": res.job_title,
        "email_sender": res.email_sender,
        "email_subject": (res.email_subject or "")[:300],
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
        "terminal_breakdown": _terminal_breakdown(conn),
        "canonical_failure_groups": _canonical_failure_counts(conn),
        "infrastructure_concentration": _infrastructure_concentration(conn),
        "worker_health": _worker_health(conn),
        "dedup_protection": _dedup_protection_summary(conn),
        "infrastructure_evidence": _infrastructure_evidence_summary(conn),
        "recent_result_events": _recent_result_event_summary(conn),
        "crash": _crash_summary(conn, sample_limit=sample_limit),
        "email_reconcile": _email_summary(
            conn,
            home_db_path=home_db_path,
            min_score=min_score,
            sample_limit=sample_limit,
        ),
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
    lines.append("terminal breakdown:")
    for bucket, data in sorted(report.get("terminal_breakdown", {}).items()):
        lines.append(f"  {bucket}: {data.get('count', 0)}")
    lines.append("canonical failure groups:")
    for group, count in report.get("canonical_failure_groups", {}).items():
        lines.append(f"  {group}: {count}")
    concentration = report.get("infrastructure_concentration", {})
    if concentration.get("workers"):
        lines.append("infrastructure concentration:")
        lines.append("  workers: " + ", ".join(
            f"{item['name']}={item['count']}" for item in concentration["workers"][:5]
        ))
        lines.append("  hosts: " + ", ".join(
            f"{item['name']}={item['count']}" for item in concentration.get("hosts", [])[:5]
        ))
    workers = report.get("worker_health", [])
    if workers:
        lines.append("worker health:")
        for worker in workers:
            lines.append(
                f"  {worker['worker_id']}: state={worker['state']} alive={worker['alive']} "
                f"age_seconds={worker['age_seconds']:.0f} infrastructure_failures={worker['infrastructure_failures']}"
            )
    dedup = report.get("dedup_protection", {})
    if dedup.get("total"):
        lines.append(
            "dedup protection: "
            f"total={dedup.get('total', 0)} strong_metadata={dedup.get('strong_metadata', 0)} "
            f"weak_metadata={dedup.get('weak_metadata', 0)} "
            f"no_queue_evidence={dedup.get('no_queue_evidence', 0)} "
            f"conflicting_evidence={dedup.get('conflicting_evidence', 0)}"
        )
    evidence = report.get("infrastructure_evidence", {})
    if sum(evidence.values()):
        lines.append(
            "infrastructure evidence: "
            f"no_event={evidence.get('no_event', 0)} "
            f"missing_tool_calls={evidence.get('missing_tool_calls', 0)} "
            f"zero_tool_calls={evidence.get('zero_tool_calls', 0)} "
            f"tool_touched={evidence.get('tool_touched', 0)}"
        )
    events = report.get("recent_result_events", [])
    if events:
        lines.append("recent result events (24h): " + ", ".join(
            f"{event['source']}/{event['status']}={event['count']}" for event in events
        ))
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
