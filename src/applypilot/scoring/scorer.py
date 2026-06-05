"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_preference_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- First decide whether the role is in the candidate's target lane. Target lanes are Chief of Staff, Strategy & Operations, Business Operations, GTM/Sales Ops, Business Development, Strategic Partnerships, Solutions Consulting, Value Engineering, complex B2B sales / Account Executive roles, Product/Program/Project Management, Strategic Finance, analytics/data roles, and Sales/Solutions Engineer.
- For commercial/sales roles, credit earned evidence of revenue responsibility, complex negotiation, stakeholder trust, business-case thinking, bid/proposal ownership, partnerships, and analytical credibility. Do not require formal quota history for a strong score when the role rewards judgment, relationship building, and selling complex products.
- Penalize wrong-lane roles heavily. Retail/store manager, grocery, cashier, restaurant, warehouse, driving, clinical, teaching, trades, and frontline service jobs should usually score 1-3 even if they mention operations, leadership, or customer service.
- Do not give 6+ for generic overlap alone. A 6+ requires both resume relevance AND role-lane relevance.
- Weight technical/analytical skills when the role actually needs them: Python, SQL, ML, automation, financial modeling, dashboards, CRM/Salesforce, and executive operations.
- Consider transferable experience, but be realistic about experience level, industry, and seniority.
- Prefer title/domain fit over keyword overlap. A Chief of Staff role with some gaps should rank above an unrelated store manager role with generic management keywords.

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


def _preference_profile_prompt(preference_profile: dict | None) -> str:
    """Render saved human review preferences for the scorer prompt."""
    if not preference_profile:
        return ""

    prompt_summary = preference_profile.get("promptSummary")
    if isinstance(prompt_summary, str) and prompt_summary.strip():
        text = prompt_summary.strip()
    else:
        compact = {
            "summary": preference_profile.get("summary", {}),
            "positiveSignals": preference_profile.get("positiveSignals", [])[:12],
            "negativeSignals": preference_profile.get("negativeSignals", [])[:12],
            "fitMapRules": preference_profile.get("fitMapRules", [])[:14],
            "examples": preference_profile.get("examples", {}),
        }
        import json
        text = json.dumps(compact, ensure_ascii=True, indent=2)

    if "HUMAN JOB PREFERENCE PROFILE" not in text:
        text = (
            "HUMAN JOB PREFERENCE PROFILE\n"
            "Use this as calibration from prior human reviews. "
            "It should adjust scoring when it conflicts with keyword overlap.\n"
            f"{text}"
        )
    if len(text) > 12000:
        text = f"{text[:12000]}\n...[truncated]"
    return text


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def score_job(resume_text: str, job: dict, preference_profile: dict | None = None) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    preference_prompt = _preference_profile_prompt(preference_profile)
    user_parts = [f"RESUME:\n{resume_text}"]
    if preference_prompt:
        user_parts.append(preference_prompt)
    user_parts.append(f"JOB POSTING:\n{job_text}")

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": "\n\n---\n\n".join(user_parts)},
    ]

    try:
        client = get_client(stage="score")
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        parsed = _parse_score_response(response)
        parsed["model"] = client.model
        parsed["provider"] = client.provider_name
        return parsed
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}", "error": str(e)}


def _persist_score(conn, result: dict, job_url: str) -> None:
    """Persist one score result immediately so interrupted runs can resume."""
    now = datetime.now(timezone.utc).isoformat()
    score = int(result.get("score") or 0)
    if result.get("error") or score <= 0:
        conn.execute(
            """
            UPDATE jobs
               SET score_error = ?,
                   score_error_at = ?,
                   score_attempts = COALESCE(score_attempts, 0) + 1,
                   fit_score = NULL,
                   scored_at = NULL
             WHERE url = ?
            """,
            (result.get("error") or result.get("reasoning") or "LLM scoring error", now, job_url),
        )
        conn.commit()
        return

    conn.execute(
        """
        UPDATE jobs
           SET fit_score = ?,
               score_reasoning = ?,
               score_error = NULL,
               score_error_at = NULL,
               score_attempts = COALESCE(score_attempts, 0) + 1,
               score_model = ?,
               score_provider = ?,
               scored_at = ?
         WHERE url = ?
        """,
        (
            score,
            f"{result['keywords']}\n{result['reasoning']}",
            result.get("model"),
            result.get("provider"),
            now,
            job_url,
        ),
    )
    conn.commit()


def _score_worker_count(workers: int | None = None) -> int:
    if workers is None:
        workers = os.environ.get("APPLYPILOT_SCORE_WORKERS", "1")
    try:
        return max(1, int(workers))
    except (TypeError, ValueError):
        return 1


def run_scoring(limit: int = 0, rescore: bool = False, workers: int | None = None) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    preference_profile = load_preference_profile()
    if preference_profile:
        source = preference_profile.get("source", {})
        reviewed = preference_profile.get("summary", {}).get("reviewedJobs") or source.get("reviewedJobs")
        log.info("Loaded human preference profile for scoring (%s reviewed jobs).", reviewed or "unknown")
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    worker_count = _score_worker_count(workers)
    log.info("Scoring %d jobs with %d worker(s)...", len(jobs), worker_count)
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    def score_one(job: dict) -> tuple[dict, dict]:
        result = score_job(resume_text, job, preference_profile=preference_profile)
        result["url"] = job["url"]
        return job, result

    if worker_count == 1:
        iterator = map(score_one, jobs)
    else:
        executor = ThreadPoolExecutor(max_workers=worker_count)
        futures = [executor.submit(score_one, job) for job in jobs]
        iterator = (future.result() for future in as_completed(futures))

    try:
        for job, result in iterator:
            _persist_score(conn, result, result["url"])

            completed += 1

            if int(result.get("score") or 0) == 0:
                errors += 1
            else:
                results.append(result)

            log.info(
                "[%d/%d] score=%d  %s",
                completed, len(jobs), int(result.get("score") or 0), job.get("title", "?")[:60],
            )
    finally:
        if worker_count > 1:
            executor.shutdown(wait=False, cancel_futures=True)

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
