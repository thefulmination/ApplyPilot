"""Pre-apply readiness checks for jobs that look ready in the database."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from applypilot import config
from applypilot.database import get_connection


BLOCKER_CODES = {
    "invalid_application_url",
    "manual_ats",
    "missing_resume_pdf",
    "already_applied",
    "duplicate_application_target",
}


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _is_http_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _resume_pdf_path(row: dict[str, Any]) -> Path | None:
    resume_path = row.get("tailored_resume_path")
    if not resume_path:
        return None
    return Path(resume_path).with_suffix(".pdf")


def _application_target(row: dict[str, Any]) -> str:
    return (row.get("application_url") or row.get("url") or "").strip()


def _query_candidates(
    *,
    min_score: int,
    limit: int,
    job_ref: str | None = None,
) -> list[dict[str, Any]]:
    conn = get_connection()
    params: list[Any] = []
    where = [
        "j.tailored_resume_path IS NOT NULL",
        "j.applied_at IS NULL",
        "(j.apply_status IS NULL OR j.apply_status IN ('failed', 'resume_ready', 'shortlisted'))",
    ]

    if job_ref:
        ref = job_ref.strip()
        like = f"%{ref.split('?')[0].rstrip('/')}%"
        where.append("(j.url = ? OR j.application_url = ? OR j.url LIKE ? OR j.application_url LIKE ?)")
        params.extend([ref, ref, like, like])
    else:
        where.append("COALESCE(j.audit_score, j.fit_score, 0) >= ?")
        params.append(min_score)

    sql = f"""
        SELECT j.*,
               COALESCE(j.audit_score, j.fit_score) AS score,
               a.status AS tracked_status
        FROM jobs j
        LEFT JOIN applications a ON a.job_url = j.url
        WHERE {' AND '.join(where)}
          AND (a.status IS NULL OR a.status NOT IN ('applied', 'rejected', 'closed', 'withdrawn'))
        ORDER BY COALESCE(j.audit_score, j.fit_score) DESC, j.fit_score DESC, j.discovered_at DESC
    """
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return [_row_to_dict(row) for row in conn.execute(sql, params).fetchall()]


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def evaluate_candidate(
    row: dict[str, Any],
    *,
    duplicate_targets: set[str] | None = None,
    stale_days: int = 21,
) -> dict[str, Any]:
    """Return a readiness record for one candidate row."""
    issues: list[dict[str, str]] = []
    target = _application_target(row)

    if not _is_http_url(target):
        issues.append(_issue("blocker", "invalid_application_url", "Application URL is missing or not absolute."))

    if config.is_manual_ats(target):
        issues.append(_issue("blocker", "manual_ats", "Configured as manual ATS."))

    pdf_path = _resume_pdf_path(row)
    if not pdf_path or not pdf_path.exists():
        issues.append(_issue("blocker", "missing_resume_pdf", "Tailored resume PDF is missing."))

    if row.get("applied_at") or row.get("tracked_status") == "applied":
        issues.append(_issue("blocker", "already_applied", "Application already recorded as applied."))

    if duplicate_targets and target in duplicate_targets:
        issues.append(_issue("blocker", "duplicate_application_target", "Another ready job points to the same application URL."))

    cover_path = row.get("cover_letter_path")
    if not cover_path:
        issues.append(_issue("warning", "missing_cover_letter", "No cover letter saved; agent may need to draft or skip."))
    elif not Path(cover_path).with_suffix(".pdf").exists():
        issues.append(_issue("warning", "missing_cover_letter_pdf", "Cover letter text exists but PDF is missing."))

    discovered_at = _parse_datetime(row.get("discovered_at"))
    if stale_days > 0 and discovered_at:
        now = datetime.now(timezone.utc)
        if discovered_at.tzinfo is None:
            discovered_at = discovered_at.replace(tzinfo=timezone.utc)
        age_days = (now - discovered_at).days
        if age_days > stale_days:
            issues.append(_issue("warning", "stale_job", f"Discovered {age_days} days ago."))

    severity = "ready"
    if any(issue["severity"] == "blocker" for issue in issues):
        severity = "blocked"
    elif issues:
        severity = "warning"

    return {
        "url": row.get("url"),
        "application_url": target,
        "title": row.get("title"),
        "company": row.get("company") or row.get("site"),
        "site": row.get("site"),
        "score": row.get("score"),
        "severity": severity,
        "issues": issues,
    }


def collect_preapply_checks(
    *,
    min_score: int = 7,
    limit: int = 50,
    stale_days: int = 21,
    job_ref: str | None = None,
) -> list[dict[str, Any]]:
    """Evaluate jobs that would be considered ready to apply."""
    rows = _query_candidates(min_score=min_score, limit=limit, job_ref=job_ref)
    target_counts = Counter(_application_target(row) for row in rows if _application_target(row))
    duplicate_targets = {target for target, count in target_counts.items() if count > 1}
    return [
        evaluate_candidate(row, duplicate_targets=duplicate_targets, stale_days=stale_days)
        for row in rows
    ]


def summarize_checks(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize readiness results for CLI display and gates."""
    issue_counts: Counter[str] = Counter()
    blockers = 0
    warnings = 0
    ready = 0

    for check in checks:
        if check["severity"] == "blocked":
            blockers += 1
        elif check["severity"] == "warning":
            warnings += 1
        else:
            ready += 1
        for issue in check["issues"]:
            issue_counts[issue["code"]] += 1

    return {
        "checked": len(checks),
        "ready": ready,
        "warnings": warnings,
        "blocked": blockers,
        "issue_counts": dict(sorted(issue_counts.items())),
    }
