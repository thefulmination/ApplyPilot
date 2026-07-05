"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import logging
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# antigravity: python-scorer-integration-v1
from applypilot import database
from applypilot.config import RESUME_PATH, load_preference_profile, KNOWLEDGE_GRAPH_PROMPT_PATH
from applypilot.database import MIN_GOOD_DESCRIPTION_CHARS
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
VERDICT: [ONE plain-English sentence beginning with "Good fit -", "Partial fit -", or "Not a fit -", stating the single most important reason for the score: the decisive strength or the decisive gap.]
REASONING: [2-3 sentences in plain English: what matches, what is missing, and why the score lands where it does.]"""


# Recommendation calibration is produced by an external engine ("brainstorm"),
# so treat its content defensively: cap sizes and tolerate wrong-typed fields.
_MAX_PREFERENCE_CHARS = 12000
# 32k covers the current compact KG pack; +~2.8k input tokens costs about
# $0.0008/job at the DeepSeek input rate cited in the 2026-07-04 audit.
_MAX_KNOWLEDGE_GRAPH_CHARS = 32000
_MAX_DESCRIPTION_CHARS = int(os.getenv("APPLYPILOT_SCORE_DESC_CAP", "15000"))
DESCRIPTION_TRUNCATION_MARKER = "\n[...TRUNCATED: description exceeded limit...]"
DESCRIPTION_OMISSION_MARKER = "[...middle of description omitted...]"
REQUIREMENTS_MARKER_RE = re.compile(
    r"(?i)(requirements|qualifications|what you.ll need|what we.re looking for|who you are|must[- ]haves?)"
)
# Fields the scorer calibrates on; used to detect a present-but-empty profile
# (a likely schema mismatch with the recommendation engine).
_PREFERENCE_FIELDS = ("promptSummary", "summary", "positiveSignals",
                      "negativeSignals", "fitMapRules", "examples")
_RESCORE_QUERY = (
    "SELECT * FROM jobs WHERE full_description IS NOT NULL "
    f"AND LENGTH(full_description) >= {MIN_GOOD_DESCRIPTION_CHARS} "
    "AND duplicate_of_url IS NULL"
)


def _as_list(value) -> list:
    """Return value if it is a list, else an empty list (tolerate bad input)."""
    return value if isinstance(value, list) else []


def _preference_profile_prompt(preference_profile: dict | None) -> str:
    """Render saved human review preferences for the scorer prompt."""
    if not isinstance(preference_profile, dict):
        return ""

    prompt_summary = preference_profile.get("promptSummary")
    if isinstance(prompt_summary, str) and prompt_summary.strip():
        text = prompt_summary.strip()
    else:
        compact = {
            "summary": preference_profile.get("summary", {}),
            "positiveSignals": _as_list(preference_profile.get("positiveSignals"))[:12],
            "negativeSignals": _as_list(preference_profile.get("negativeSignals"))[:12],
            "fitMapRules": _as_list(preference_profile.get("fitMapRules"))[:14],
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
    if len(text) > _MAX_PREFERENCE_CHARS:
        text = f"{text[:_MAX_PREFERENCE_CHARS]}\n...[truncated]"
    return text


def select_description(full_description, cap=None) -> str:
    desc = full_description or ""
    cap = int(cap or _MAX_DESCRIPTION_CHARS)
    if len(desc) <= cap:
        return desc

    match = REQUIREMENTS_MARKER_RE.search(desc)
    if not match:
        return desc[:cap] + DESCRIPTION_TRUNCATION_MARKER

    joiner = f"\n{DESCRIPTION_OMISSION_MARKER}\n"
    tail = desc[match.start():]
    min_head = min(500, cap)
    available = max(0, cap - len(joiner))
    if len(tail) >= available - min_head:
        head_budget = min_head
        tail_budget = max(0, available - head_budget)
        tail = tail[:tail_budget]
    else:
        head_budget = max(min_head, available - len(tail))
    head = desc[:head_budget]
    return f"{head}{joiner}{tail}{DESCRIPTION_TRUNCATION_MARKER}"


def build_score_prompt_text(resume_text, job, preference_profile=None, knowledge_graph_prompt=None) -> str:
    """The combined prompt text for a single-string backend (e.g. `codex exec`).
    Same instructions + context as score_job, flattened to one prompt."""
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job.get('site', '')}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{select_description(job.get('full_description'))}"
    )
    parts = [SCORE_PROMPT, f"RESUME:\n{resume_text}"]
    pref = _preference_profile_prompt(preference_profile)
    if pref:
        parts.append(pref)
    if knowledge_graph_prompt:
        parts.append(knowledge_graph_prompt)
    parts.append(f"JOB POSTING:\n{job_text}")
    return "\n\n---\n\n".join(parts)


def _load_knowledge_graph_prompt() -> str | None:
    try:
        if not KNOWLEDGE_GRAPH_PROMPT_PATH.exists():
            return None
        kg_prompt = KNOWLEDGE_GRAPH_PROMPT_PATH.read_text(encoding="utf-8")
        if kg_prompt and len(kg_prompt) > _MAX_KNOWLEDGE_GRAPH_CHARS:
            log.error(
                "Knowledge graph prompt is %d chars; truncating to %d to control "
                "token cost (it is injected into every scoring call).",
                len(kg_prompt), _MAX_KNOWLEDGE_GRAPH_CHARS,
            )
            database.record_scoring_context_event(
                "kg_prompt_truncated",
                json.dumps({
                    "path": str(KNOWLEDGE_GRAPH_PROMPT_PATH),
                    "original_chars": len(kg_prompt),
                    "cap": _MAX_KNOWLEDGE_GRAPH_CHARS,
                }),
            )
            kg_prompt = kg_prompt[:_MAX_KNOWLEDGE_GRAPH_CHARS] + "\n...[truncated]"
        if not (kg_prompt or "").strip():
            return None
        return kg_prompt
    except Exception as e:
        log.warning("Could not read knowledge graph prompt from %s: %s", KNOWLEDGE_GRAPH_PROMPT_PATH, e)
        return None


def load_score_context() -> dict:
    """Resume / preference profile / KG prompt from the same sources run_scoring reads.
    For the frontier pass + any standalone scorer caller."""
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    preference_profile = load_preference_profile()
    kg_prompt = _load_knowledge_graph_prompt()
    return {"resume_text": resume_text, "preference_profile": preference_profile, "kg_prompt": kg_prompt}


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Tolerant of the model omitting labels, reordering them, or wrapping a
    field across multiple lines.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "verdict": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    verdict = ""
    reasoning_parts: list[str] = []
    field = None  # the multi-line field currently being accumulated

    for raw in response.split("\n"):
        line = raw.strip()
        head = line.lstrip("-*# \t").upper()
        if head.startswith("SCORE:"):
            field = None
            try:
                score = max(1, min(10, int(re.search(r"\d+", line).group())))
            except (AttributeError, ValueError):
                score = 0
        elif head.startswith("KEYWORDS:"):
            field = None
            keywords = line.split(":", 1)[1].strip()
        elif head.startswith("VERDICT:"):
            field = "verdict"
            verdict = line.split(":", 1)[1].strip()
        elif head.startswith("REASONING:"):
            field = "reasoning"
            reasoning_parts.append(line.split(":", 1)[1].strip())
        elif field == "reasoning":
            reasoning_parts.append(line)
        elif field == "verdict" and line:
            verdict = f"{verdict} {line}".strip()

    reasoning = " ".join(p for p in (s.strip() for s in reasoning_parts) if p).strip()

    # Fallbacks when the model ignores the format entirely.
    if not reasoning:
        leftover = [line.strip() for line in response.split("\n")
                    if line.strip() and not re.match(r"(?i)^[-*#\s]*(SCORE|KEYWORDS|VERDICT)\s*:", line)]
        reasoning = " ".join(leftover).strip() or response.strip()
    if not verdict and reasoning:
        verdict = re.split(r"(?<=[.!?])\s+", reasoning, maxsplit=1)[0][:300]

    return {"score": score, "keywords": keywords, "verdict": verdict, "reasoning": reasoning}


def score_job(
    resume_text: str,
    job: dict,
    preference_profile: dict | None = None,
    knowledge_graph_prompt: str | None = None,
    provider: str | None = None
) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, company/site, location, full_description.
        preference_profile: Human preference calibration data.
        knowledge_graph_prompt: Factual knowledge graph prompt pack.
        provider: Optional LLM provider override.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job.get('company') or job.get('site') or 'Unknown'}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{select_description(job.get('full_description'))}"
    )

    preference_prompt = _preference_profile_prompt(preference_profile)
    user_parts = [f"RESUME:\n{resume_text}"]
    if preference_prompt:
        user_parts.append(preference_prompt)
    if knowledge_graph_prompt:
        user_parts.append(knowledge_graph_prompt)
    user_parts.append(f"JOB POSTING:\n{job_text}")

    system_content = SCORE_PROMPT
    if knowledge_graph_prompt:
        system_content += (
            "\n\nKNOWLEDGE GRAPH INSTRUCTIONS:\n"
            "- Use the provided KNOWLEDGE GRAPH to calibrate factual background.\n"
            "- Cite specific graph node IDs (e.g., education:stevens-quantitative-finance-bs or context:ggg:...) in your reasoning for how requirements are met.\n"
            "- Classify requirements as qualified (direct/high-confidence evidence), transferable (adjacent evidence), stretch (plausible but indirect with limits), missing (no evidence or gap), or unclear."
        )

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "\n\n---\n\n".join(user_parts)},
    ]

    try:
        client = get_client(stage="score", provider_override=provider)
        prompt_variant = os.getenv("SCORE_PROMPT_VARIANT")
        response = client.chat(messages, max_tokens=512, temperature=0.2, stage="score", prompt_variant=prompt_variant)
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
               fit_verdict = ?,
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
            result.get("verdict") or "",
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


def _score_preflight_liveness_enabled() -> bool:
    return os.environ.get("APPLYPILOT_SCORE_PREFLIGHT_LIVENESS", "").strip().lower() not in (
        "0", "false", "no", "off")


def _score_preflight_max_age_days() -> int:
    raw = os.environ.get("APPLYPILOT_SCORE_PREFLIGHT_MAX_AGE_DAYS", "1")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 1


def _score_preflight_workers(default_workers: int) -> int:
    raw = os.environ.get("APPLYPILOT_SCORE_PREFLIGHT_WORKERS")
    if raw is None:
        return max(1, min(16, default_workers if default_workers > 1 else 16))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 16


def _dead_liveness_urls(conn, urls: list[str]) -> set[str]:
    dead: set[str] = set()
    for i in range(0, len(urls), 900):
        chunk = urls[i:i + 900]
        if not chunk:
            continue
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT url FROM jobs WHERE liveness_status='dead' AND url IN ({placeholders})",
            chunk,
        ).fetchall()
        dead.update(row["url"] for row in rows)
    return dead


def _preflight_scoring_liveness(conn, jobs: list[dict], worker_count: int) -> list[dict]:
    if not _score_preflight_liveness_enabled() or not jobs:
        return jobs
    from applypilot.apply import liveness

    summary = liveness.verify_candidate_rows(
        conn,
        jobs,
        max_age_days=_score_preflight_max_age_days(),
        workers=_score_preflight_workers(worker_count),
        dry_run=False,
    )
    by_status = summary.get("by_status", {}) or {}
    if summary.get("checked", 0):
        log.info(
            "Pre-score liveness checked %d candidates (live=%d, dead=%d, uncertain=%d; skipped fresh=%d).",
            summary.get("checked", 0),
            by_status.get("live", 0),
            by_status.get("dead", 0),
            by_status.get("uncertain", 0),
            summary.get("skipped_fresh", 0),
        )

    dead_urls = _dead_liveness_urls(conn, [str(job.get("url") or "") for job in jobs])
    if not dead_urls:
        return jobs
    filtered = [job for job in jobs if job.get("url") not in dead_urls]
    log.info("Skipped %d dead postings before scoring.", len(jobs) - len(filtered))
    return filtered


def run_scoring(limit: int = 0, rescore: bool = False, workers: int | None = None) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    # load_preference_profile() already guarantees a dict-or-None (it tolerates
    # malformed recommendation-engine output), so the .get chains below are safe.
    preference_profile = load_preference_profile()
    if preference_profile:
        summary = preference_profile.get("summary")
        source = preference_profile.get("source")
        reviewed = None
        if isinstance(summary, dict):
            reviewed = summary.get("reviewedJobs")
        if reviewed is None and isinstance(source, dict):
            reviewed = source.get("reviewedJobs")
        log.info("Loaded human preference profile for scoring (%s reviewed jobs).", reviewed or "unknown")
        # Present but with none of the fields we calibrate on => likely a schema
        # mismatch with the recommendation engine. Surface it rather than
        # silently scoring as if uncalibrated.
        if not any(preference_profile.get(k) for k in _PREFERENCE_FIELDS):
            log.warning(
                "Preference profile has none of the expected fields %s; "
                "check the recommendation engine output schema.", _PREFERENCE_FIELDS,
            )

    # antigravity: python-scorer-integration-v1
    knowledge_graph_prompt = _load_knowledge_graph_prompt()
    if knowledge_graph_prompt:
        log.info("Loaded job knowledge graph prompt for scoring calibration (%d chars).",
                 len(knowledge_graph_prompt))

    conn = get_connection()

    if rescore:
        query = f"{_RESCORE_QUERY} {database.llm_stage_liveness_sql()}"
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
    jobs = _preflight_scoring_liveness(conn, jobs, worker_count)
    if not jobs:
        log.info("No live unscored jobs with descriptions found after liveness preflight.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    log.info("Scoring %d jobs with %d worker(s)...", len(jobs), worker_count)
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    # antigravity: python-scorer-integration-v1
    def score_one(job: dict) -> tuple[dict, dict]:
        result = score_job(
            resume_text,
            job,
            preference_profile=preference_profile,
            knowledge_graph_prompt=knowledge_graph_prompt
        )
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
