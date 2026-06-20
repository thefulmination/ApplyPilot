"""Export saved jobs for review and ranking.

The database remains the source of truth. This module writes a human-readable
snapshot of the current jobs table so the user can audit every discovered job,
its score, and the job description used for scoring and tailoring.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from applypilot.config import JOB_EXPORT_DIR
from applypilot.database import get_connection


CSV_COLUMNS = [
    "rank",
    "audit_score",
    "decision_source",
    "decision_verdict",
    "external_decision_score",
    "role_fit_score",
    "audit_label",
    "audit_flags",
    "audit_reason",
    "fit_gap_category",
    "fit_gap_severity",
    "resume_fixability",
    "recommended_action",
    "fit_diagnosis",
    "fit_score",
    "score_model",
    "score_provider",
    "title",
    "site",
    "location",
    "salary",
    "url",
    "application_url",
    "dedupe_key",
    "duplicate_of_url",
    "duplicate_reason",
    "duplicate_detected_at",
    "description_file",
    "full_description_chars",
    "score_reasoning",
    "discovered_at",
    "detail_scraped_at",
    "scored_at",
    "diagnosed_at",
    "tailored_resume_path",
    "cover_letter_path",
    "apply_status",
    "applied_at",
]

DB_COLUMNS = [
    "url",
    "title",
    "salary",
    "description",
    "location",
    "site",
    "strategy",
    "discovered_at",
    "full_description",
    "application_url",
    "dedupe_key",
    "duplicate_of_url",
    "duplicate_reason",
    "duplicate_detected_at",
    "detail_scraped_at",
    "detail_error",
    "fit_score",
    "score_reasoning",
    "score_model",
    "score_provider",
    "scored_at",
    "role_fit_score",
    "audit_score",
    "audit_label",
    "audit_flags",
    "audit_reason",
    "audited_at",
    "decision_source",
    "decision_verdict",
    "external_decision_score",
    "decision_at",
    "fit_gap_category",
    "fit_gap_severity",
    "resume_fixability",
    "recommended_action",
    "fit_diagnosis",
    "fit_diagnosis_json",
    "diagnosis_model",
    "diagnosis_provider",
    "diagnosed_at",
    "tailored_resume_path",
    "tailored_at",
    "tailor_attempts",
    "cover_letter_path",
    "cover_letter_at",
    "cover_attempts",
    "applied_at",
    "apply_status",
    "apply_error",
    "apply_attempts",
    "agent_id",
    "last_attempted_at",
    "apply_duration_ms",
    "apply_task_id",
    "verification_confidence",
]


def _safe_part(value: Any, fallback: str = "job", max_len: int = 80) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9._ -]+", "", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._- ")
    return (text[:max_len].strip("._- ") or fallback)


def _score_label(score: Any) -> str:
    if score is None:
        return "unscored"
    try:
        return f"{int(score):02d}"
    except (TypeError, ValueError):
        return "unknown"


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _description_filename(index: int, job: dict[str, Any]) -> str:
    score = _score_label(job.get("audit_score") if job.get("audit_score") is not None else job.get("fit_score"))
    site = _safe_part(job.get("site"), "unknown", 35)
    title = _safe_part(job.get("title"), "untitled", 90)
    return f"{index:05d}_score-{score}_{site}_{title}.txt"


def _write_job_text(path: Path, rank: int, job: dict[str, Any]) -> None:
    description = job.get("full_description") or job.get("description") or ""
    lines = [
        f"Rank: {rank}",
        f"Audited score: {job.get('audit_score') if job.get('audit_score') is not None else ''}",
        f"Role fit score: {job.get('role_fit_score') if job.get('role_fit_score') is not None else ''}",
        f"Audit label: {job.get('audit_label') or ''}",
        f"Audit flags: {job.get('audit_flags') or ''}",
        f"Audit reason: {job.get('audit_reason') or ''}",
        f"Fit gap category: {job.get('fit_gap_category') or ''}",
        f"Fit gap severity: {job.get('fit_gap_severity') or ''}",
        f"Resume fixability: {job.get('resume_fixability') or ''}",
        f"Recommended action: {job.get('recommended_action') or ''}",
        f"Fit diagnosis: {job.get('fit_diagnosis') or ''}",
        f"Fit score: {job.get('fit_score') if job.get('fit_score') is not None else 'Unscored'}",
        f"Score model: {job.get('score_provider') or ''}/{job.get('score_model') or ''}",
        f"Diagnosis model: {job.get('diagnosis_provider') or ''}/{job.get('diagnosis_model') or ''}",
        f"Title: {job.get('title') or ''}",
        f"Company/source: {job.get('site') or ''}",
        f"Location: {job.get('location') or ''}",
        f"Salary: {job.get('salary') or ''}",
        f"URL: {job.get('url') or ''}",
        f"Application URL: {job.get('application_url') or ''}",
        f"Dedupe key: {job.get('dedupe_key') or ''}",
        f"Duplicate of: {job.get('duplicate_of_url') or ''}",
        f"Duplicate reason: {job.get('duplicate_reason') or ''}",
        f"Duplicate detected at: {job.get('duplicate_detected_at') or ''}",
        f"Discovered at: {job.get('discovered_at') or ''}",
        f"Enriched at: {job.get('detail_scraped_at') or ''}",
        f"Scored at: {job.get('scored_at') or ''}",
        f"Diagnosed at: {job.get('diagnosed_at') or ''}",
        f"Tailored resume: {job.get('tailored_resume_path') or ''}",
        f"Cover letter: {job.get('cover_letter_path') or ''}",
        f"Apply status: {job.get('apply_status') or ''}",
        "",
        "Score reasoning:",
        job.get("score_reasoning") or "",
        "",
        "Job description:",
        description,
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _build_where(
    min_score: int | None,
    scored_only: bool,
    full_description_only: bool,
) -> tuple[str, list[Any]]:
    clauses = ["1=1"]
    params: list[Any] = []

    if min_score is not None:
        clauses.append("fit_score >= ?")
        params.append(min_score)
    elif scored_only:
        clauses.append("fit_score IS NOT NULL")

    if full_description_only:
        clauses.append("full_description IS NOT NULL AND full_description != ''")

    return " AND ".join(clauses), params


def export_jobs(
    output_dir: str | Path | None = None,
    min_score: int | None = None,
    scored_only: bool = False,
    full_description_only: bool = False,
    limit: int = 0,
    write_descriptions: bool = True,
) -> dict[str, Any]:
    """Export saved jobs to CSV, JSONL, summary JSON, and text descriptions.

    Args:
        output_dir: Destination folder. If omitted, a timestamped folder is
            created under .applypilot/job_exports.
        min_score: Optional minimum fit score. Omit to export all jobs.
        scored_only: Export only scored jobs when min_score is omitted.
        full_description_only: Export only jobs with full descriptions.
        limit: Maximum number of jobs to export. 0 means no limit.
        write_descriptions: Whether to write one text file per job.

    Returns:
        Dictionary with output paths and row counts.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = Path(output_dir) if output_dir else JOB_EXPORT_DIR / timestamp
    descriptions_dir = destination / "descriptions"
    destination.mkdir(parents=True, exist_ok=True)
    if write_descriptions:
        descriptions_dir.mkdir(parents=True, exist_ok=True)

    conn = get_connection()
    where, params = _build_where(min_score, scored_only, full_description_only)
    sql = (
        f"SELECT {', '.join(DB_COLUMNS)} FROM jobs "
        f"WHERE {where} "
        "ORDER BY (COALESCE(audit_score, fit_score) IS NULL), "
        "COALESCE(audit_score, fit_score) DESC, "
        "fit_score DESC, discovered_at DESC, title"
    )
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)

    rows = [_row_to_dict(row) for row in conn.execute(sql, params).fetchall()]

    csv_path = destination / "jobs.csv"
    jsonl_path = destination / "jobs.jsonl"
    summary_path = destination / "summary.json"

    score_counts = Counter(
        str(job["fit_score"]) if job.get("fit_score") is not None else "unscored"
        for job in rows
    )
    audit_label_counts = Counter(job.get("audit_label") or "unaudited" for job in rows)
    recommended_action_counts = Counter(job.get("recommended_action") or "undiagnosed" for job in rows)
    site_counts = Counter(job.get("site") or "Unknown" for job in rows)

    description_count = 0
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file, jsonl_path.open(
        "w", encoding="utf-8"
    ) as jsonl_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        for index, job in enumerate(rows, start=1):
            description_file = ""
            if write_descriptions:
                description_file = _description_filename(index, job)
                _write_job_text(descriptions_dir / description_file, index, job)
                description_count += 1

            exported = dict(job)
            exported["rank"] = index
            exported["description_file"] = str(Path("descriptions") / description_file) if description_file else ""
            jsonl_file.write(json.dumps(exported, ensure_ascii=False) + "\n")

            writer.writerow({
                "rank": index,
                "audit_score": job.get("audit_score"),
                "decision_source": job.get("decision_source"),
                "decision_verdict": job.get("decision_verdict"),
                "external_decision_score": job.get("external_decision_score"),
                "role_fit_score": job.get("role_fit_score"),
                "audit_label": job.get("audit_label"),
                "audit_flags": job.get("audit_flags"),
                "audit_reason": job.get("audit_reason"),
                "fit_gap_category": job.get("fit_gap_category"),
                "fit_gap_severity": job.get("fit_gap_severity"),
                "resume_fixability": job.get("resume_fixability"),
                "recommended_action": job.get("recommended_action"),
                "fit_diagnosis": job.get("fit_diagnosis"),
                "fit_score": job.get("fit_score"),
                "score_model": job.get("score_model"),
                "score_provider": job.get("score_provider"),
                "title": job.get("title"),
                "site": job.get("site"),
                "location": job.get("location"),
                "salary": job.get("salary"),
                "url": job.get("url"),
                "application_url": job.get("application_url"),
                "description_file": exported["description_file"],
                "full_description_chars": len(job.get("full_description") or ""),
                "score_reasoning": job.get("score_reasoning"),
                "discovered_at": job.get("discovered_at"),
                "detail_scraped_at": job.get("detail_scraped_at"),
                "scored_at": job.get("scored_at"),
                "diagnosed_at": job.get("diagnosed_at"),
                "tailored_resume_path": job.get("tailored_resume_path"),
                "cover_letter_path": job.get("cover_letter_path"),
                "apply_status": job.get("apply_status"),
                "applied_at": job.get("applied_at"),
            })

    summary = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "output_dir": str(destination),
        "filters": {
            "min_score": min_score,
            "scored_only": scored_only,
            "full_description_only": full_description_only,
            "limit": limit,
        },
        "jobs_exported": len(rows),
        "description_files": description_count,
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "summary_path": str(summary_path),
        "descriptions_dir": str(descriptions_dir) if write_descriptions else None,
        "score_counts": dict(sorted(score_counts.items(), reverse=True)),
        "audit_label_counts": dict(audit_label_counts.most_common()),
        "recommended_action_counts": dict(recommended_action_counts.most_common()),
        "site_counts": dict(site_counts.most_common()),
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
