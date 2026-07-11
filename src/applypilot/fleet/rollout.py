"""Evidence-gated Workday rollout orchestration."""
from __future__ import annotations

import json
import hashlib
import secrets
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable
from urllib.parse import urlparse


@dataclass(frozen=True)
class RolloutRecord:
    stage: str
    job_url: str
    tenant: str
    status: str
    reason: str
    cost_usd: float = 0.0
    false_success: bool = False
    duplicate: bool = False
    unsupported_claim: bool = False
    observed: bool = False
    metadata: dict | None = None


@dataclass(frozen=True)
class ExpansionThresholds:
    min_shadow: int = 10
    min_canary: int = 5
    max_false_success: int = 0
    max_duplicates: int = 0
    max_unsupported_claims: int = 0
    max_exception_rate: float = 0.20
    max_average_cost_usd: float = 0.25


@dataclass(frozen=True)
class ExpansionDecision:
    allowed: bool
    reasons: tuple[str, ...]
    metrics: dict


def ensure_rollout_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS apply_rollout_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stage TEXT NOT NULL,
            job_url TEXT NOT NULL,
            tenant TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            cost_usd REAL NOT NULL DEFAULT 0,
            false_success INTEGER NOT NULL DEFAULT 0,
            duplicate INTEGER NOT NULL DEFAULT 0,
            unsupported_claim INTEGER NOT NULL DEFAULT 0,
            observed INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_apply_rollout_stage ON apply_rollout_runs(stage, id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workday_canary_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL,
            job_url TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(token_hash, job_url)
        )
    """)
    conn.commit()


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def issue_canary_approval(conn: sqlite3.Connection, job_urls: Iterable[str], *,
                          ttl_minutes: int = 30) -> str:
    """Issue a single-use-per-job token for an operator-reviewed canary batch."""
    ensure_rollout_table(conn)
    urls = tuple(dict.fromkeys(str(url).strip() for url in job_urls if str(url).strip()))
    if not urls:
        raise ValueError("no_review_ready_jobs")
    token = secrets.token_urlsafe(32)
    digest = _token_hash(token)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=max(1, min(int(ttl_minutes), 120)))
    conn.executemany("""
        INSERT INTO workday_canary_approvals
            (token_hash,job_url,expires_at,created_at) VALUES (?,?,?,?)
    """, [(digest, url, expires.isoformat(), now.isoformat()) for url in urls])
    conn.commit()
    return token


def consume_canary_approval(conn: sqlite3.Connection, token: str, job_url: str) -> bool:
    """Atomically consume approval for one exact job URL."""
    ensure_rollout_table(conn)
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute("""
        UPDATE workday_canary_approvals
           SET used_at=?
         WHERE token_hash=? AND job_url=? AND used_at IS NULL AND expires_at>?
    """, (now, _token_hash(token), str(job_url), now))
    conn.commit()
    return int(cursor.rowcount or 0) == 1


def latest_review_ready_urls(conn: sqlite3.Connection, jobs: Iterable[dict], *,
                             max_age_hours: int = 24) -> list[str]:
    """Return exact candidate URLs with fresh persisted review-ready evidence."""
    ensure_rollout_table(conn)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, max_age_hours))).isoformat()
    ready: list[str] = []
    for job in jobs:
        url = job.get("application_url") or job["url"]
        row = conn.execute("""
            SELECT status,reason,created_at FROM apply_rollout_runs
             WHERE stage='workday_canary_prepare' AND job_url=?
             ORDER BY id DESC LIMIT 1
        """, (url,)).fetchone()
        if row and row["status"] == "dry_run" and row["reason"] == "review_ready" and row["created_at"] >= cutoff:
            ready.append(url)
    return ready


def persist_record(conn: sqlite3.Connection, record: RolloutRecord) -> None:
    ensure_rollout_table(conn)
    conn.execute(
        """INSERT INTO apply_rollout_runs
           (stage,job_url,tenant,status,reason,cost_usd,false_success,duplicate,
            unsupported_claim,observed,metadata_json,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (record.stage, record.job_url, record.tenant, record.status, record.reason,
         float(record.cost_usd), int(record.false_success), int(record.duplicate),
         int(record.unsupported_claim), int(record.observed),
         json.dumps(record.metadata or {}, sort_keys=True), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def load_records(conn: sqlite3.Connection) -> list[RolloutRecord]:
    ensure_rollout_table(conn)
    rows = conn.execute("SELECT * FROM apply_rollout_runs ORDER BY id").fetchall()
    return [RolloutRecord(
        stage=row["stage"], job_url=row["job_url"], tenant=row["tenant"],
        status=row["status"], reason=row["reason"], cost_usd=float(row["cost_usd"]),
        false_success=bool(row["false_success"]), duplicate=bool(row["duplicate"]),
        unsupported_claim=bool(row["unsupported_claim"]), observed=bool(row["observed"]),
        metadata=json.loads(row["metadata_json"] or "{}"),
    ) for row in rows]


def select_workday_candidates(conn: sqlite3.Connection, *, limit: int,
                              canary: bool = False) -> list[dict]:
    rows = conn.execute("""
        SELECT url, application_url, title, company, tailored_resume_path, source_board, site
          FROM jobs
         WHERE lower(COALESCE(application_url,url,'')) LIKE '%workday%'
           AND liveness_status = 'live'
           AND COALESCE(apply_status,'') NOT IN ('applied','in_progress')
         ORDER BY COALESCE(audit_score,fit_score,0) DESC, discovered_at DESC
         LIMIT ?
    """, (max(1, int(limit) * 5),)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        target = item.get("application_url") or item["url"]
        host = (urlparse(target).hostname or "").lower()
        item["target_host"] = host
        item["tenant_validated"] = False
        if canary:
            tenant = conn.execute(
                "SELECT status,session_state FROM ats_tenants WHERE host=?", (host,)
            ).fetchone()
            item["tenant_validated"] = bool(
                tenant and tenant["status"] in {"supervised", "trusted"}
                and tenant["session_state"] == "ready"
            )
        result.append(item)
        if len(result) >= limit:
            break
    return result


def fresh_workday_candidates(conn: sqlite3.Connection, *, limit: int,
                             canary: bool = False, probe_fn=None) -> list[dict]:
    """Require a fresh public-API liveness result immediately before rollout."""
    if probe_fn is None:
        from applypilot.apply.liveness import probe_url as probe_fn
    candidates = select_workday_candidates(conn, limit=max(int(limit) * 10, limit), canary=canary)
    live = []
    for job in candidates:
        target = job.get("application_url") or job["url"]
        status, _reason = probe_fn(target)
        if status != "live":
            continue
        live.append(job)
        if len(live) >= limit:
            break
    return live


def _record(stage: str, job: dict, result, *, observed=False) -> RolloutRecord:
    metadata = dict(getattr(result, "metadata", {}) or {})
    return RolloutRecord(
        stage=stage,
        job_url=job.get("application_url") or job["url"],
        tenant=job.get("target_host") or job.get("tenant") or "unknown",
        status=str(getattr(result, "status", "parked")),
        reason=str(getattr(result, "reason", "missing_result")),
        cost_usd=float(metadata.get("cost_usd") or 0),
        observed=observed,
        metadata=metadata,
    )


def run_workday_shadow(jobs: Iterable[dict], executor: Callable, *, limit: int = 10,
                       record_fn: Callable[[RolloutRecord], None] | None = None) -> list[RolloutRecord]:
    records = []
    for job in list(jobs)[:max(0, min(int(limit), 10))]:
        result = executor(job, submit=False)
        record = _record("workday_shadow", job, result)
        records.append(record)
        if record_fn: record_fn(record)
    return records


def run_supervised_canary(jobs: Iterable[dict], executor: Callable, *,
                          observer_approve: Callable[[dict], bool], limit: int = 5,
                          record_fn: Callable[[RolloutRecord], None] | None = None) -> list[RolloutRecord]:
    records = []
    for job in list(jobs)[:max(0, min(int(limit), 5))]:
        if not job.get("tenant_validated"):
            record = RolloutRecord("workday_canary", job.get("application_url") or job["url"],
                                   job.get("target_host") or "unknown", "parked",
                                   "tenant_not_validated")
        elif not observer_approve(job):
            record = RolloutRecord("workday_canary", job.get("application_url") or job["url"],
                                   job.get("target_host") or "unknown", "parked",
                                   "observer_not_present")
        else:
            record = _record("workday_canary", job, executor(job, submit=True), observed=True)
        records.append(record)
        if record_fn: record_fn(record)
    return records


def run_canary_prepare(jobs: Iterable[dict], executor: Callable, *, limit: int = 5,
                       record_fn: Callable[[RolloutRecord], None] | None = None) -> list[RolloutRecord]:
    """Prove five validated tenant profiles can reach review without submitting."""
    records = []
    for job in list(jobs)[:max(0, min(int(limit), 5))]:
        if not job.get("tenant_validated"):
            record = RolloutRecord("workday_canary_prepare", job.get("application_url") or job["url"],
                                   job.get("target_host") or "unknown", "parked",
                                   "tenant_not_validated")
        else:
            result = executor(job, submit=False)
            record = _record("workday_canary_prepare", job, result)
        records.append(record)
        if record_fn: record_fn(record)
    return records


def evaluate_expansion(records: Iterable[RolloutRecord],
                       thresholds: ExpansionThresholds | None = None) -> ExpansionDecision:
    thresholds = thresholds or ExpansionThresholds()
    latest: dict[tuple[str, str], RolloutRecord] = {}
    for record in records:
        latest[(record.stage, record.job_url)] = record
    rows = list(latest.values())
    shadow = [row for row in rows if row.stage == "workday_shadow"]
    canary = [row for row in rows if row.stage == "workday_canary"]
    false_success = sum(row.false_success for row in rows)
    duplicates = sum(row.duplicate for row in rows)
    unsupported = sum(row.unsupported_claim for row in rows)
    exceptions = sum(
        (row.stage == "workday_shadow" and row.status != "dry_run")
        or (row.stage == "workday_canary" and row.status != "applied")
        for row in rows
    )
    exception_rate = exceptions / len(rows) if rows else 1.0
    average_cost = sum(row.cost_usd for row in rows) / len(rows) if rows else float("inf")
    reasons = []
    if len(shadow) < thresholds.min_shadow: reasons.append("insufficient_shadow_count")
    if len(canary) < thresholds.min_canary: reasons.append("insufficient_canary_count")
    if any(not row.observed for row in canary): reasons.append("unobserved_canary")
    if false_success > thresholds.max_false_success: reasons.append("false_success_threshold")
    if duplicates > thresholds.max_duplicates: reasons.append("duplicate_threshold")
    if unsupported > thresholds.max_unsupported_claims: reasons.append("unsupported_claim_threshold")
    if exception_rate > thresholds.max_exception_rate: reasons.append("exception_rate_threshold")
    if average_cost > thresholds.max_average_cost_usd: reasons.append("cost_threshold")
    metrics = {
        "shadow_count": len(shadow), "canary_count": len(canary),
        "false_success": false_success, "duplicates": duplicates,
        "unsupported_claims": unsupported, "exception_rate": exception_rate,
        "average_cost_usd": average_cost,
    }
    return ExpansionDecision(not reasons, tuple(reasons), metrics)
