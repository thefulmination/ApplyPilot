"""LLM-powered fit diagnosis for scored jobs.

Scoring answers "how good is this fit?" Diagnosis answers "why is this fit
weak or strong, and is the gap fixable with resume positioning?"
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from applypilot.config import RESUME_PATH
from applypilot.database import get_connection, init_db
from applypilot.llm import get_client

log = logging.getLogger(__name__)


GAP_CATEGORIES = {
    "wrong_role_lane",
    "missing_hard_requirement",
    "missing_domain_experience",
    "missing_seniority",
    "missing_sales_evidence",
    "missing_technical_skill",
    "resume_signal_gap",
    "false_negative_possible",
    "strong_transferable_fit",
    "diagnosis_error",
}

GAP_SEVERITIES = {"low", "medium", "high"}
RESUME_FIXABILITY = {"yes", "maybe", "no"}
RECOMMENDED_ACTIONS = {
    "apply",
    "tailor_resume",
    "create_new_resume_lane",
    "manual_review",
    "ignore",
}
CONFIDENCE_LEVELS = {"low", "medium", "high"}


DIAGNOSIS_PROMPT = """You are a job-fit diagnosis judge.

You will receive a candidate resume, one job posting, and ApplyPilot's existing scores.
Your task is not to rescore the job. Your task is to explain WHY the fit is strong or weak and whether the weakness is fixable by better resume positioning.

Candidate positioning to remember:
- Current COO-style construction operating experience can transfer strongly to Chief of Staff, business operations, strategy, GTM operations, strategic partnerships, and complex relationship-driven sales roles.
- Do not dismiss the candidate just because the industry differs if the role rewards operating judgment, executive communication, revenue responsibility, analytics, and stakeholder trust.
- Do not pretend a gap is fixable when the posting requires licensed credentials, deep software engineering, clinical credentials, hard quota history, or a highly specific technical background that is not in the resume.
- Separate a true experience gap from a resume signal gap. A resume signal gap means the candidate may have relevant experience, but the current resume does not make it obvious enough.

Return ONLY valid JSON with exactly these keys:
{
  "gap_category": "wrong_role_lane | missing_hard_requirement | missing_domain_experience | missing_seniority | missing_sales_evidence | missing_technical_skill | resume_signal_gap | false_negative_possible | strong_transferable_fit",
  "gap_severity": "low | medium | high",
  "resume_fixability": "yes | maybe | no",
  "recommended_action": "apply | tailor_resume | create_new_resume_lane | manual_review | ignore",
  "summary": "2-4 sentence plain-English explanation",
  "resume_evidence": ["specific evidence from resume that helps"],
  "missing_or_weak_evidence": ["specific missing evidence or weak signals"],
  "resume_changes": ["specific resume changes if fixable"],
  "confidence": "low | medium | high"
}
"""


@dataclass
class FitDiagnosis:
    gap_category: str
    gap_severity: str
    resume_fixability: str
    recommended_action: str
    summary: str
    resume_evidence: list[str]
    missing_or_weak_evidence: list[str]
    resume_changes: list[str]
    confidence: str
    model: str | None = None
    provider: str | None = None

    def as_json(self) -> str:
        return json.dumps({
            "gap_category": self.gap_category,
            "gap_severity": self.gap_severity,
            "resume_fixability": self.resume_fixability,
            "recommended_action": self.recommended_action,
            "summary": self.summary,
            "resume_evidence": self.resume_evidence,
            "missing_or_weak_evidence": self.missing_or_weak_evidence,
            "resume_changes": self.resume_changes,
            "confidence": self.confidence,
            "model": self.model,
            "provider": self.provider,
        }, ensure_ascii=False)


def _clean_choice(value: Any, allowed: set[str], fallback: str) -> str:
    cleaned = str(value or "").strip().lower().replace(" ", "_")
    return cleaned if cleaned in allowed else fallback


def _clean_list(value: Any, max_items: int = 5) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            items.append(text[:300])
        if len(items) >= max_items:
            break
    return items


def _json_from_response(response: str) -> dict[str, Any]:
    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("diagnosis response was not a JSON object")
    return parsed


def _normalize_diagnosis(parsed: dict[str, Any], *, model: str | None, provider: str | None) -> FitDiagnosis:
    return FitDiagnosis(
        gap_category=_clean_choice(parsed.get("gap_category"), GAP_CATEGORIES, "resume_signal_gap"),
        gap_severity=_clean_choice(parsed.get("gap_severity"), GAP_SEVERITIES, "medium"),
        resume_fixability=_clean_choice(parsed.get("resume_fixability"), RESUME_FIXABILITY, "maybe"),
        recommended_action=_clean_choice(parsed.get("recommended_action"), RECOMMENDED_ACTIONS, "manual_review"),
        summary=str(parsed.get("summary") or "No diagnosis summary returned.").strip()[:1200],
        resume_evidence=_clean_list(parsed.get("resume_evidence")),
        missing_or_weak_evidence=_clean_list(parsed.get("missing_or_weak_evidence")),
        resume_changes=_clean_list(parsed.get("resume_changes")),
        confidence=_clean_choice(parsed.get("confidence"), CONFIDENCE_LEVELS, "medium"),
        model=model,
        provider=provider,
    )


def _error_diagnosis(error: Exception) -> FitDiagnosis:
    return FitDiagnosis(
        gap_category="diagnosis_error",
        gap_severity="medium",
        resume_fixability="maybe",
        recommended_action="manual_review",
        summary=f"Fit diagnosis failed: {error}",
        resume_evidence=[],
        missing_or_weak_evidence=[],
        resume_changes=[],
        confidence="low",
    )


def diagnose_job(resume_text: str, job: dict[str, Any]) -> FitDiagnosis:
    """Diagnose one scored job against the resume."""
    job_text = (
        f"TITLE: {job.get('title') or ''}\n"
        f"COMPANY: {job.get('site') or ''}\n"
        f"LOCATION: {job.get('location') or 'N/A'}\n"
        f"LLM_FIT_SCORE: {job.get('fit_score')}\n"
        f"AUDIT_SCORE: {job.get('audit_score')}\n"
        f"AUDIT_LABEL: {job.get('audit_label') or ''}\n"
        f"AUDIT_FLAGS: {job.get('audit_flags') or ''}\n"
        f"AUDIT_REASON: {job.get('audit_reason') or ''}\n"
        f"SCORE_REASONING: {job.get('score_reasoning') or ''}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or job.get('description') or '')[:7000]}"
    )
    messages = [
        {"role": "system", "content": DIAGNOSIS_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING AND SCORES:\n{job_text}"},
    ]

    try:
        client = get_client(stage="diagnose")
        response = client.chat(messages, max_tokens=1400, temperature=0.1)
        return _normalize_diagnosis(
            _json_from_response(response),
            model=client.model,
            provider=client.provider_name,
        )
    except Exception as exc:
        log.error("LLM error diagnosing job '%s': %s", job.get("title", "?"), exc)
        return _error_diagnosis(exc)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _pending_jobs_sql(rediagnose: bool) -> str:
    where = "fit_score IS NOT NULL AND full_description IS NOT NULL AND duplicate_of_url IS NULL"
    if not rediagnose:
        where += (
            " AND (diagnosed_at IS NULL "
            "OR (scored_at IS NOT NULL AND diagnosed_at < scored_at) "
            "OR (audited_at IS NOT NULL AND diagnosed_at < audited_at))"
        )
    return f"""
        SELECT *
          FROM jobs
         WHERE {where}
         ORDER BY
           CASE
             WHEN audit_flags LIKE '%missed_priority_role%' THEN 0
             WHEN audit_label = 'review' THEN 1
             WHEN audit_label = 'low' THEN 2
             WHEN audit_label = 'exclude' THEN 3
             WHEN COALESCE(audit_score, fit_score, 0) BETWEEN 5 AND 8 THEN 4
             ELSE 5
           END,
           COALESCE(audit_score, fit_score) DESC,
           discovered_at DESC
    """


def run_diagnostics(limit: int = 0, rediagnose: bool = False) -> dict[str, Any]:
    """Diagnose pending scored jobs and persist each result immediately."""
    init_db()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    sql = _pending_jobs_sql(rediagnose)
    params: list[Any] = []
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)

    jobs = [_row_to_dict(row) for row in conn.execute(sql, params).fetchall()]
    if not jobs:
        log.info("No jobs needing fit diagnosis.")
        return {"diagnosed": 0, "errors": 0, "elapsed": 0.0, "labels": {}}

    log.info("Diagnosing %d scored jobs sequentially...", len(jobs))
    t0 = time.time()
    diagnosed = 0
    errors = 0
    label_counts: dict[str, int] = {}

    for job in jobs:
        diagnosis = diagnose_job(resume_text, job)
        if diagnosis.gap_category == "diagnosis_error":
            errors += 1
        label_counts[diagnosis.recommended_action] = label_counts.get(diagnosis.recommended_action, 0) + 1
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE jobs
               SET fit_gap_category = ?,
                   fit_gap_severity = ?,
                   resume_fixability = ?,
                   recommended_action = ?,
                   fit_diagnosis = ?,
                   fit_diagnosis_json = ?,
                   diagnosis_model = ?,
                   diagnosis_provider = ?,
                   diagnosed_at = ?
             WHERE url = ?
            """,
            (
                diagnosis.gap_category,
                diagnosis.gap_severity,
                diagnosis.resume_fixability,
                diagnosis.recommended_action,
                diagnosis.summary,
                diagnosis.as_json(),
                diagnosis.model,
                diagnosis.provider,
                now,
                job["url"],
            ),
        )
        conn.commit()
        diagnosed += 1
        log.info(
            "[%d/%d] diagnosis=%s/%s  %s",
            diagnosed,
            len(jobs),
            diagnosis.gap_category,
            diagnosis.recommended_action,
            (job.get("title") or "?")[:60],
        )

    elapsed = time.time() - t0
    log.info("Done: %d diagnosed in %.1fs", diagnosed, elapsed)
    return {
        "diagnosed": diagnosed,
        "errors": errors,
        "elapsed": elapsed,
        "labels": label_counts,
    }
