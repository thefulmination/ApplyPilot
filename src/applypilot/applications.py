"""Application tracking helpers.

The jobs table remains the source for discovered roles and generated files.
The applications tables track human workflow: applied dates, status changes,
follow-ups, contacts, and notes.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot.config import APPLICATION_EXPORT_DIR
from applypilot.database import get_connection


VALID_STATUSES = {
    "shortlisted",
    "resume_ready",
    "auth_required",
    "assisted",
    "applied",
    "followed_up",
    "recruiter_screen",
    "hiring_manager",
    "assessment",
    "final",
    "offer",
    "rejected",
    "closed",
    "failed",
    "withdrawn",
}

ACTIVE_STATUSES = {
    "shortlisted",
    "resume_ready",
    "auth_required",
    "assisted",
    "applied",
    "followed_up",
    "recruiter_screen",
    "hiring_manager",
    "assessment",
    "final",
    "offer",
    "failed",
}

TRACKER_COLUMNS = [
    "status",
    "applied_at",
    "next_follow_up_at",
    "title",
    "company",
    "score",
    "channel",
    "application_url",
    "job_url",
    "resume_path",
    "cover_letter_path",
    "contact_name",
    "contact_email",
    "contact_url",
    "notes",
    "last_status_at",
    "created_at",
    "updated_at",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_status(status: str) -> str:
    normalized = status.strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "interview": "recruiter_screen",
        "screen": "recruiter_screen",
        "hm": "hiring_manager",
        "hiring_manager_screen": "hiring_manager",
        "follow_up": "followed_up",
        "followup": "followed_up",
        "login_required": "auth_required",
        "account_required": "auth_required",
        "2fa_required": "auth_required",
        "mfa_required": "auth_required",
        "manual_required": "assisted",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status '{status}'. Valid statuses: {', '.join(sorted(VALID_STATUSES))}"
        )
    return normalized


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _find_job(conn, job_ref: str) -> dict[str, Any] | None:
    ref = job_ref.strip()
    like = f"%{ref.split('?')[0].rstrip('/')}%"
    row = conn.execute("""
        SELECT *
        FROM jobs
        WHERE url = ?
           OR application_url = ?
           OR url LIKE ?
           OR application_url LIKE ?
        ORDER BY
            CASE WHEN url = ? OR application_url = ? THEN 0 ELSE 1 END,
            COALESCE(audit_score, fit_score) DESC NULLS LAST, discovered_at DESC
        LIMIT 1
    """, (ref, ref, like, like, ref, ref)).fetchone()
    return _row_to_dict(row) if row else None


def _score(job: dict[str, Any]) -> Any:
    return job.get("audit_score") if job.get("audit_score") is not None else job.get("fit_score")


def record_application(
    job_ref: str,
    status: str = "applied",
    channel: str = "manual",
    notes: str | None = None,
    applied_at: str | None = None,
    next_follow_up_at: str | None = None,
    contact_name: str | None = None,
    contact_email: str | None = None,
    contact_url: str | None = None,
    update_job: bool = True,
) -> dict[str, Any]:
    """Create or update an application tracker record for a job."""
    status = _normalize_status(status)
    conn = get_connection()
    job = _find_job(conn, job_ref)
    if not job:
        raise ValueError(f"No job found for: {job_ref}")

    now = _now()
    if status == "applied":
        applied_at = applied_at or job.get("applied_at") or now

    existing = conn.execute(
        "SELECT * FROM applications WHERE job_url = ?",
        (job["url"],),
    ).fetchone()

    existing_applied_at = existing["applied_at"] if existing else None
    final_applied_at = applied_at or existing_applied_at

    if existing:
        conn.execute("""
            UPDATE applications
            SET title = ?, company = ?, source = ?, source_url = ?,
                application_url = ?, status = ?, channel = ?,
                applied_at = ?, last_status_at = ?, next_follow_up_at = COALESCE(?, next_follow_up_at),
                resume_path = ?, cover_letter_path = ?,
                contact_name = COALESCE(?, contact_name),
                contact_email = COALESCE(?, contact_email),
                contact_url = COALESCE(?, contact_url),
                notes = CASE
                    WHEN ? IS NULL OR ? = '' THEN notes
                    WHEN notes IS NULL OR notes = '' THEN ?
                    ELSE notes || char(10) || ?
                END,
                updated_at = ?
            WHERE job_url = ?
        """, (
            job.get("title"), job.get("company") or job.get("site"),
            job.get("source_board") or job.get("site"), job.get("url"),
            job.get("application_url"), status, channel, final_applied_at, now,
            next_follow_up_at, job.get("tailored_resume_path"), job.get("cover_letter_path"),
            contact_name, contact_email, contact_url, notes, notes, notes, notes,
            now, job["url"],
        ))
    else:
        conn.execute("""
            INSERT INTO applications (
                job_url, title, company, source, source_url, application_url,
                status, channel, applied_at, last_status_at, next_follow_up_at,
                resume_path, cover_letter_path, contact_name, contact_email,
                contact_url, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job["url"], job.get("title"), job.get("company") or job.get("site"),
            job.get("source_board") or job.get("site"),
            job.get("url"), job.get("application_url"), status, channel,
            final_applied_at, now, next_follow_up_at, job.get("tailored_resume_path"),
            job.get("cover_letter_path"), contact_name, contact_email, contact_url,
            notes, now, now,
        ))

    conn.execute("""
        INSERT INTO application_events (
            job_url, status, event_type, channel, happened_at, notes
        )
        VALUES (?, ?, 'status_change', ?, ?, ?)
    """, (job["url"], status, channel, now, notes))

    if update_job:
        if status == "applied":
            conn.execute("""
                UPDATE jobs
                SET apply_status = 'applied',
                    applied_at = COALESCE(applied_at, ?),
                    apply_error = NULL,
                    agent_id = NULL
                WHERE url = ?
            """, (final_applied_at or now, job["url"]))
        elif status == "failed":
            conn.execute("""
                UPDATE jobs
                SET apply_status = 'failed',
                    apply_error = COALESCE(?, 'manual'),
                    agent_id = NULL
                WHERE url = ?
            """, (notes, job["url"]))
        else:
            conn.execute("""
                UPDATE jobs
                SET apply_status = ?,
                    agent_id = NULL
                WHERE url = ?
            """, (status, job["url"]))

    conn.commit()
    return get_application(job["url"]) or {}


def get_application(job_ref: str) -> dict[str, Any] | None:
    conn = get_connection()
    job = _find_job(conn, job_ref)
    job_url = job["url"] if job else job_ref
    row = conn.execute("""
        SELECT a.*,
               COALESCE(j.audit_score, j.fit_score) AS score,
               j.fit_score,
               j.audit_score,
               j.audit_label,
               j.location,
               j.salary
        FROM applications a
        LEFT JOIN jobs j ON j.url = a.job_url
        WHERE a.job_url = ?
    """, (job_url,)).fetchone()
    return _row_to_dict(row) if row else None


def get_application_handoff(job_ref: str) -> dict[str, Any]:
    """Return the data needed to finish a gated application manually."""
    conn = get_connection()
    job = _find_job(conn, job_ref)
    if not job:
        raise ValueError(f"No job found for: {job_ref}")
    return {
        "job_url": job.get("url"),
        "application_url": job.get("application_url") or job.get("url"),
        "title": job.get("title"),
        "company": job.get("company") or job.get("site"),
        "score": _score(job),
        "resume_path": job.get("tailored_resume_path"),
        "resume_pdf_path": str(Path(job["tailored_resume_path"]).with_suffix(".pdf"))
        if job.get("tailored_resume_path") else None,
        "cover_letter_path": job.get("cover_letter_path"),
        "cover_letter_pdf_path": str(Path(job["cover_letter_path"]).with_suffix(".pdf"))
        if job.get("cover_letter_path") else None,
    }


def list_applications(
    status: str | None = None,
    active_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    conn = get_connection()
    clauses = ["1=1"]
    params: list[Any] = []
    if status:
        clauses.append("a.status = ?")
        params.append(_normalize_status(status))
    elif active_only:
        placeholders = ",".join("?" * len(ACTIVE_STATUSES))
        clauses.append(f"a.status IN ({placeholders})")
        params.extend(sorted(ACTIVE_STATUSES))

    sql = f"""
        SELECT a.*,
               COALESCE(j.audit_score, j.fit_score) AS score,
               j.fit_score,
               j.audit_score,
               j.audit_label,
               j.location,
               j.salary
        FROM applications a
        LEFT JOIN jobs j ON j.url = a.job_url
        WHERE {' AND '.join(clauses)}
        ORDER BY
            CASE WHEN a.next_follow_up_at IS NULL THEN 1 ELSE 0 END,
            a.next_follow_up_at,
            a.applied_at DESC,
            a.updated_at DESC
    """
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return [_row_to_dict(row) for row in conn.execute(sql, params).fetchall()]


def list_ready_to_apply(min_score: int = 7, limit: int = 50) -> list[dict[str, Any]]:
    conn = get_connection()
    sql = """
        SELECT j.*,
               COALESCE(j.audit_score, j.fit_score) AS score
        FROM jobs j
        LEFT JOIN applications a ON a.job_url = j.url
        WHERE j.tailored_resume_path IS NOT NULL
          AND j.duplicate_of_url IS NULL
          AND j.applied_at IS NULL
          AND (j.apply_status IS NULL OR j.apply_status IN ('failed', 'resume_ready', 'shortlisted'))
          AND COALESCE(j.audit_score, j.fit_score, 0) >= ?
          AND (a.status IS NULL OR a.status NOT IN ('applied', 'rejected', 'closed', 'withdrawn'))
        ORDER BY COALESCE(j.audit_score, j.fit_score) DESC, j.fit_score DESC, j.discovered_at DESC
    """
    params: list[Any] = [min_score]
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return [_row_to_dict(row) for row in conn.execute(sql, params).fetchall()]


def application_events(job_ref: str, limit: int = 25) -> list[dict[str, Any]]:
    conn = get_connection()
    job = _find_job(conn, job_ref)
    job_url = job["url"] if job else job_ref
    sql = """
        SELECT *
        FROM application_events
        WHERE job_url = ?
        ORDER BY happened_at DESC, id DESC
    """
    params: list[Any] = [job_url]
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return [_row_to_dict(row) for row in conn.execute(sql, params).fetchall()]


def export_applications(output_dir: str | Path | None = None) -> dict[str, Any]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = Path(output_dir) if output_dir else APPLICATION_EXPORT_DIR / timestamp
    destination.mkdir(parents=True, exist_ok=True)

    rows = list_applications(limit=0)
    csv_path = destination / "applications.csv"
    jsonl_path = destination / "applications.jsonl"
    summary_path = destination / "summary.json"

    with csv_path.open("w", newline="", encoding="utf-8") as csv_file, jsonl_path.open(
        "w", encoding="utf-8"
    ) as jsonl_file:
        writer = csv.DictWriter(csv_file, fieldnames=TRACKER_COLUMNS)
        writer.writeheader()
        for row in rows:
            exported = {key: row.get(key) for key in TRACKER_COLUMNS}
            writer.writerow(exported)
            jsonl_file.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "applications_exported": len(rows),
        "output_dir": str(destination),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "summary_path": str(summary_path),
        "statuses": {
            status: sum(1 for row in rows if row.get("status") == status)
            for status in sorted({row.get("status") for row in rows if row.get("status")})
        },
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
