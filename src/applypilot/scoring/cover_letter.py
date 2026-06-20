"""Cover letter generation: LLM-powered, profile-driven, with validation.

Generates concise, engineering-voice cover letters tailored to specific job
postings. All personal data (name, skills, achievements) comes from the user's
profile at runtime. No hardcoded personal information.
"""

import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone

from applypilot.config import COVER_LETTER_DIR, RESUME_PATH, load_profile, load_resume_strategy
from applypilot.database import get_connection
from applypilot.llm import get_client
from applypilot.scoring.validator import (
    BANNED_WORDS,
    LLM_LEAK_PHRASES,
    sanitize_text,
    validate_cover_letter,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _model_is_available(model: str) -> bool:
    model_l = model.lower()
    if model_l.startswith("deepseek-"):
        return bool(os.environ.get("DEEPSEEK_API_KEY"))
    if model_l.startswith("gemini-"):
        return bool(os.environ.get("GEMINI_API_KEY"))
    if model_l.startswith(("gpt-", "o1", "o3", "o4")):
        return bool(os.environ.get("OPENAI_API_KEY"))
    return True


def _safe_job_prefix(job: dict) -> str:
    """Readable, stable filename prefix that avoids collisions for duplicate titles."""
    safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
    safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
    fingerprint_source = job.get("url") or "|".join(
        str(job.get(key, "")) for key in ("site", "title", "location")
    )
    fingerprint = hashlib.sha1(fingerprint_source.encode("utf-8")).hexdigest()[:8]
    return f"{safe_site}_{safe_title}_{fingerprint}"


def _build_cover_model_plan() -> list[dict]:
    primary_client = get_client(stage="cover")
    plan: list[dict] = [
        {
            "role": "primary",
            "model": primary_client.model,
            "client": primary_client,
            "attempts": None,
        }
    ]
    fallback_model = (
        os.environ.get("LLM_COVER_FALLBACK_MODEL")
        or os.environ.get("LLM_PRO_FALLBACK_MODEL")
        or ""
    ).strip()
    if (
        fallback_model
        and fallback_model != primary_client.model
        and _model_is_available(fallback_model)
    ):
        plan.append(
            {
                "role": "fallback",
                "model": fallback_model,
                "client": get_client(model_override=fallback_model),
                "attempts": max(1, _env_int("COVER_FALLBACK_ATTEMPTS", 1)),
            }
        )
    return plan


# ── Prompt Builder (profile-driven) ──────────────────────────────────────

def _format_resume_strategy(strategy: dict) -> str:
    if not strategy:
        return ""
    facts = strategy.get("protected_facts", []) or []
    positioning = strategy.get("chief_of_staff_positioning", []) or []
    lines: list[str] = []
    if facts:
        lines.append("Use these real proof points where relevant:")
        lines.extend(f"- {fact}" for fact in facts)
    if positioning:
        lines.append("Preferred positioning:")
        lines.extend(f"- {item}" for item in positioning)
    return "\n".join(lines)


def _build_cover_letter_prompt(profile: dict, resume_strategy: dict | None = None) -> str:
    """Build the cover letter system prompt from the user's profile.

    All personal data, skills, and sign-off name come from the profile.
    """
    personal = profile.get("personal", {})
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Preferred name for the sign-off (falls back to full name)
    sign_off_name = personal.get("preferred_name") or personal.get("full_name", "")

    # Flatten all allowed skills
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "the tools listed in the resume"

    # Real metrics from resume_facts
    real_metrics = resume_facts.get("real_metrics", [])
    preserved_projects = resume_facts.get("preserved_projects", [])

    # Build achievement examples for the prompt
    projects_hint = ""
    if preserved_projects:
        projects_hint = f"\nKnown projects to reference: {', '.join(preserved_projects)}"

    metrics_hint = ""
    if real_metrics:
        metrics_hint = f"\nReal metrics to use: {', '.join(real_metrics)}"

    # Build the full banned list from the validator so the prompt stays in sync
    # with what will actually be rejected — the validator checks all of these.
    all_banned = ", ".join(f'"{w}"' for w in BANNED_WORDS)
    leak_banned = ", ".join(f'"{p}"' for p in LLM_LEAK_PHRASES)
    strategy_block = _format_resume_strategy(resume_strategy or {})

    return f"""Write a cover letter for {sign_off_name}. The goal is to get an interview.

STRUCTURE: 3 short paragraphs. Under 250 words. Every sentence must earn its place.

RESUME STRATEGY:
{strategy_block or "Use the strongest resume facts for the target job."}

PARAGRAPH 1 (2-3 sentences): Open with a specific thing YOU built that solves THEIR problem. Not "I'm excited about this role." Not "This role aligns with my experience." Start with the work.

PARAGRAPH 2 (3-4 sentences): Pick 2 achievements from the resume that are MOST relevant to THIS job. Use numbers. Frame as solving their problem, not listing your accomplishments.{projects_hint}{metrics_hint}

PARAGRAPH 3 (1-2 sentences): One specific thing about the company from the job description (a product, a technical challenge, a team structure). Then close. "Happy to walk through any of this in more detail." or "Let's discuss." Nothing else.

BANNED WORDS AND PHRASES (automated validator rejects ANY of these — do not use even once):
{all_banned}

ALSO BANNED (meta-commentary the validator catches):
{leak_banned}

BANNED PUNCTUATION: No em dashes (—) or en dashes (–). Use commas or periods.

VOICE:
- Write like a real engineer emailing someone they respect. Not formal, not casual. Just direct.
- NEVER narrate or explain what you're doing. BAD: "This demonstrates my commitment to X." GOOD: Just state the fact and move on.
- NEVER hedge. BAD: "might address some of your challenges." GOOD: "solves the same problem your team is facing."
- Every sentence should contain either a number, a tool name, or a specific outcome. If it doesn't, cut it.
- Read it out loud. If it sounds like a robot wrote it, rewrite it.

FABRICATION = INSTANT REJECTION:
The candidate's real tools are ONLY: {skills_str}.
Do NOT mention ANY tool not in this list. If the job asks for tools not listed, talk about the work you did, not the tools.

Sign off: just "{sign_off_name}"

Output ONLY the letter text. No subject lines. No "Here is the cover letter:" preamble. No notes after the sign-off.
Start DIRECTLY with "Dear Hiring Manager," and end with the name."""


# ── Helpers ──────────────────────────────────────────────────────────────

def _strip_preamble(text: str) -> str:
    """Remove LLM preamble before 'Dear Hiring Manager,' if present.

    Gemini and other models sometimes output "Here is the cover letter:" or
    similar meta-commentary before the actual letter text. Strip everything
    before the first occurrence of "Dear" so the validator's start-check passes.
    """
    # Prefer a line-anchored greeting ("Dear ...") so a stray "dear" inside the
    # preamble prose can't truncate the letter mid-sentence. Fall back to the
    # first standalone "dear" only if no anchored match exists.
    m = re.search(r"(?im)^\s*dear\b", text) or re.search(r"\bdear\b", text, re.IGNORECASE)
    if m and m.start() > 0:
        return text[m.start():].lstrip()
    return text


# ── Core Generation ──────────────────────────────────────────────────────

def generate_cover_letter(
    resume_text: str, job: dict, profile: dict,
    resume_strategy: dict | None = None,
    max_retries: int = 3, validation_mode: str = "normal",
) -> str:
    """Generate a cover letter with fresh context on each retry + auto-sanitize.

    Same design as tailor_resume: fresh conversation per attempt, issues noted
    in the prompt, no conversation history stacking.

    Args:
        resume_text:      The candidate's resume text (base or tailored).
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".

    Returns:
        The cover letter text (best attempt even if validation failed).
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    avoid_notes: list[str] = []
    letter = ""
    model_plan = _build_cover_model_plan()
    cl_prompt_base = _build_cover_letter_prompt(profile, resume_strategy=resume_strategy)

    for model_entry in model_plan:
        client = model_entry["client"]
        attempts = model_entry["attempts"]
        if attempts is None:
            attempts = max_retries + 1

        for attempt in range(attempts):
            # Fresh conversation every attempt
            prompt = cl_prompt_base
            if avoid_notes:
                prompt += "\n\n## AVOID THESE ISSUES:\n" + "\n".join(
                    f"- {n}" for n in avoid_notes[-5:]
                )

            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": (
                    f"RESUME:\n{resume_text}\n\n---\n\n"
                    f"TARGET JOB:\n{job_text}\n\n"
                    "Write the cover letter:"
                )},
            ]

            letter = client.chat(messages, max_tokens=4096, temperature=0.7)
            letter = sanitize_text(letter)  # auto-fix em dashes, smart quotes
            letter = _strip_preamble(letter)  # remove any "Here is the letter:" prefix

            validation = validate_cover_letter(letter, mode=validation_mode, profile=profile)
            if validation["passed"]:
                if model_entry["role"] != "primary":
                    log.info(
                        "Cover letter fallback model succeeded: %s",
                        client.model,
                    )
                return letter

            avoid_notes.extend(validation["errors"])
            # Warnings never block — only hard errors trigger a retry
            log.debug(
                "Cover letter %s attempt %d/%d failed: %s",
                model_entry["role"], attempt + 1, attempts, validation["errors"],
            )

    return letter  # last attempt even if failed


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_cover_letters(min_score: int = 7, limit: int = 900,
                      validation_mode: str = "normal") -> dict:
    """Generate cover letters for high-scoring jobs that have tailored resumes.

    Args:
        min_score:       Minimum audited score / fit_score threshold.
        limit:           Maximum jobs to process. 0 means all eligible jobs.
        validation_mode: "strict", "normal", or "lenient".

    Returns:
        {"generated": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    resume_strategy = load_resume_strategy()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    # Fetch jobs that have tailored resumes but no cover letter yet
    query = (
        "SELECT * FROM jobs "
        "WHERE COALESCE(audit_score, fit_score) >= ? AND tailored_resume_path IS NOT NULL "
        "AND duplicate_of_url IS NULL "
        "AND full_description IS NOT NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        "AND COALESCE(cover_attempts, 0) < ? "
        "ORDER BY COALESCE(audit_score, fit_score) DESC, fit_score DESC"
    )
    params: list = [min_score, MAX_ATTEMPTS]
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)
    jobs = conn.execute(query, params).fetchall()

    if not jobs:
        log.info("No jobs needing cover letters (score >= %d).", min_score)
        return {"generated": 0, "errors": 0, "elapsed": 0.0}

    # Convert rows to dicts
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    log.info(
        "Generating cover letters for %d jobs (score >= %d)...",
        len(jobs), min_score,
    )
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    error_count = 0

    for job in jobs:
        completed += 1
        try:
            letter = generate_cover_letter(resume_text, job, profile,
                                          resume_strategy=resume_strategy,
                                          validation_mode=validation_mode)

            # Build safe filename prefix
            prefix = _safe_job_prefix(job)

            cl_path = COVER_LETTER_DIR / f"{prefix}_CL.txt"
            cl_path.write_text(letter, encoding="utf-8")

            # Generate PDF (best-effort)
            pdf_path = None
            try:
                from applypilot.scoring.pdf import convert_to_pdf
                pdf_path = str(convert_to_pdf(cl_path))
            except Exception:
                log.debug("PDF generation failed for %s", cl_path, exc_info=True)

            result = {
                "url": job["url"],
                "path": str(cl_path),
                "pdf_path": pdf_path,
                "title": job["title"],
                "site": job["site"],
            }
            results.append(result)

            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            log.info(
                "%d/%d [OK] | %.1f jobs/min | %s",
                completed, len(jobs), rate * 60, result["title"][:40],
            )
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "path": None, "pdf_path": None, "error": str(e),
            }
            error_count += 1
            results.append(result)
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)

        # Persist after every job so Ctrl+C or a later slow request does not
        # lose already-generated cover letters.
        now = datetime.now(timezone.utc).isoformat()
        if result.get("path"):
            conn.execute(
                "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, "
                "cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (result["path"], now, result["url"]),
            )
        else:
            conn.execute(
                "UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (result["url"],),
            )
        conn.commit()

    elapsed = time.time() - t0
    saved = sum(1 for r in results if r.get("path"))
    log.info("Cover letters done in %.1fs: %d generated, %d errors", elapsed, saved, error_count)

    return {
        "generated": saved,
        "errors": error_count,
        "elapsed": elapsed,
    }
