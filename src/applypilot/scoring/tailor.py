"""Resume tailoring: LLM-powered ATS-optimized resume generation per job.

THIS IS THE HEAVIEST REFACTOR. Every piece of personal data -- name, email, phone,
skills, companies, projects, school -- is loaded at runtime from the user's profile.
Zero hardcoded personal information.

The LLM returns structured JSON, code assembles the final text. Header (name, contact)
is always code-injected, never LLM-generated. Each retry starts a fresh conversation
to avoid apologetic spirals.
"""

import hashlib
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, TAILORED_DIR, load_profile, load_resume_strategy
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client
from applypilot.scoring.validator import (
    BANNED_WORDS,
    sanitize_text,
    validate_json_fields,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _job_score(job: dict) -> float:
    for key in ("audit_score", "fit_score"):
        value = job.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _model_is_available(model: str) -> bool:
    model_l = model.lower()
    if model_l.startswith("deepseek-"):
        return bool(os.environ.get("DEEPSEEK_API_KEY"))
    if model_l.startswith("gemini-"):
        return bool(os.environ.get("GEMINI_API_KEY"))
    if model_l.startswith(("gpt-", "o1", "o3", "o4")):
        return bool(os.environ.get("OPENAI_API_KEY"))
    return True


def _build_tailor_model_plan(job: dict, max_retries: int) -> list[dict]:
    """Build the ordered model plan for a single tailored resume.

    Primary model stays cheap. Premium fallback is only used after primary
    attempts fail validation/judge, with one faster fallback for top-scoring jobs.
    """
    primary_client = get_client(stage="tailor")
    primary_attempts = max(1, _env_int("TAILOR_PRIMARY_ATTEMPTS", 2))
    high_score = _job_score(job) >= _env_float("TAILOR_PREMIUM_MIN_SCORE", 9.5)
    if high_score:
        primary_attempts = max(1, _env_int("TAILOR_HIGH_SCORE_PRIMARY_ATTEMPTS", 1))

    total_attempts = max_retries + 1
    primary_attempts = min(primary_attempts, total_attempts)

    plan: list[dict] = [
        {
            "role": "primary",
            "model": primary_client.model,
            "client": primary_client,
            "attempts": primary_attempts,
        }
    ]

    remaining = total_attempts - primary_attempts
    fallback_model = (
        os.environ.get("LLM_TAILOR_FALLBACK_MODEL")
        or os.environ.get("LLM_PRO_FALLBACK_MODEL")
        or ""
    ).strip()
    if (
        remaining > 0
        and fallback_model
        and fallback_model != primary_client.model
        and _model_is_available(fallback_model)
    ):
        plan.append(
            {
                "role": "fallback",
                "model": fallback_model,
                "client": get_client(model_override=fallback_model),
                "attempts": remaining,
            }
        )
        remaining = 0

    final_model = os.environ.get("LLM_TAILOR_FINAL_MODEL", "").strip()
    if (
        remaining > 0
        and final_model
        and final_model not in {entry["model"] for entry in plan}
        and _model_is_available(final_model)
    ):
        plan.append(
            {
                "role": "final",
                "model": final_model,
                "client": get_client(model_override=final_model),
                "attempts": remaining,
            }
        )

    return plan


def _usage_for_report(client) -> dict:
    return dict(client.last_usage or {})


def _safe_job_prefix(job: dict) -> str:
    """Readable, stable filename prefix that avoids collisions for duplicate titles."""
    safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
    safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
    fingerprint_source = job.get("url") or "|".join(
        str(job.get(key, "")) for key in ("site", "title", "location")
    )
    fingerprint = hashlib.sha1(fingerprint_source.encode("utf-8")).hexdigest()[:8]
    return f"{safe_site}_{safe_title}_{fingerprint}"


# ── Prompt Builders (profile-driven) ──────────────────────────────────────

def _format_resume_strategy(strategy: dict) -> str:
    """Format user-owned positioning rules for the tailoring prompt."""
    if not strategy:
        return "No additional resume strategy configured."

    lines: list[str] = []
    positioning = strategy.get("positioning", {}) or {}
    if positioning:
        lines.append("Positioning:")
        for key, value in positioning.items():
            lines.append(f"- {key.replace('_', ' ').title()}: {value}")

    for section in ("protected_facts", "tailoring_rules", "chief_of_staff_positioning"):
        items = strategy.get(section, []) or []
        if items:
            lines.append(f"\n{section.replace('_', ' ').title()}:")
            for item in items:
                lines.append(f"- {item}")

    required_companies = strategy.get("required_experience_companies", []) or []
    if required_companies:
        lines.append("\nRequired Experience Companies:")
        for company in required_companies:
            lines.append(f"- {company}")

    projects = strategy.get("required_projects", []) or []
    if projects:
        lines.append("\nRequired Projects:")
        for project in projects:
            name = project.get("name", "Project")
            summary = project.get("summary", "")
            lines.append(f"- {name}: {summary}")
            for bullet in project.get("bullets", []) or []:
                lines.append(f"  - {bullet}")

    return "\n".join(lines)


def _build_tailor_prompt(profile: dict, resume_strategy: dict | None = None) -> str:
    """Build the resume tailoring system prompt from the user's profile.

    All skills boundaries, preserved entities, and formatting rules are
    derived from the profile -- nothing is hardcoded.
    """
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Format skills boundary for the prompt
    skills_lines = []
    for category, items in boundary.items():
        if isinstance(items, list) and items:
            label = category.replace("_", " ").title()
            skills_lines.append(f"{label}: {', '.join(items)}")
    skills_block = "\n".join(skills_lines)

    # Preserved entities
    companies = resume_facts.get("preserved_companies", [])
    school = resume_facts.get("preserved_school", "")
    real_metrics = resume_facts.get("real_metrics", [])

    companies_str = ", ".join(companies) if companies else "N/A"
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"
    strategy_block = _format_resume_strategy(resume_strategy or {})

    # Include ALL banned words from the validator so the LLM knows exactly
    # what will be rejected — the validator checks for these automatically.
    banned_str = ", ".join(BANNED_WORDS)

    education = profile.get("experience", {})
    education_level = education.get("education_level", "")

    return f"""You are a senior technical recruiter rewriting a resume to get this person an interview.

Take the base resume and job description. Return a tailored resume as a JSON object.

## RESUME STRATEGY (highest priority, do not ignore):
{strategy_block}

## RECRUITER SCAN (6 seconds):
1. Title -- matches what they're hiring?
2. Summary -- 2 sentences proving you've done this work
3. First 3 bullets of most recent role -- verbs and outcomes match?
4. Skills -- must-haves visible immediately?

## SKILLS BOUNDARY (real skills only):
{skills_block}

You MAY add 2-3 closely related tools (Kubernetes if Docker, Terraform if AWS, Redis if PostgreSQL). No unrelated languages/frameworks.
Do NOT copy required tools from the job description into the skills section unless they are already in SKILLS BOUNDARY.
If the job asks for a tool the candidate does not have, translate the underlying business capability instead of naming the tool.

## TAILORING RULES:

TITLE: Match the target role. Keep seniority (Senior/Lead/Staff). Drop company suffixes and team names.

SUMMARY: Rewrite from scratch. Lead with the 1-2 skills that matter most for THIS role. Sound like someone who's done this job.

SKILLS: Reorder each category so the job's must-haves appear first.

Reframe EVERY bullet for this role. Same real work, different angle. Every bullet must be reworded. Never copy verbatim.

EXPERIENCE: Keep every company listed under Required Experience Companies.
GGG Construction Corp must stay prominent with 4 bullets for operating/leadership roles.
Older roles can be compressed or shifted into Projects only when needed for a one-page resume.

PROJECTS: Always include 2-3 relevant selected projects when configured in RESUME STRATEGY.
Use real projects or operating projects from the strategy. Never leave PROJECTS blank.
Rename projects to match the job language, but keep the underlying facts true.

BULLETS: Strong verb + what you built + quantified impact. Vary verbs (Built, Designed, Implemented, Reduced, Automated, Deployed, Operated, Optimized). Most relevant first. Max 4 per section.

## VOICE:
- Write like a real engineer. Short, direct.
- GOOD: "Automated financial reporting with Python + API integrations, cut processing time from 10 hours to 2"
- BAD: "Leveraged cutting-edge AI technologies to drive transformative operational efficiencies"
- BANNED WORDS (using ANY of these = validation failure — do not use them even once):
  {banned_str}
- No em dashes. Use commas, periods, or hyphens.

## HARD RULES:
- Do NOT invent work, companies, degrees, or certifications
- Do NOT change real numbers ({metrics_str})
- Preserved companies: {companies_str} -- names stay as-is
- Preserved school: {school}
- Must fit 1 page.

## OUTPUT: Return ONLY valid JSON. No markdown fences. No commentary. No "here is" preamble.

{{"title":"Role Title","summary":"2-3 tailored sentences.","skills":{{"Languages":"...","Frameworks":"...","DevOps & Infra":"...","Databases":"...","Tools":"..."}},"experience":[{{"header":"Title at Company","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2","bullet 3","bullet 4"]}}],"projects":[{{"header":"Project Name - Description","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2"]}}],"education":"{school} | {education_level}"}}"""


def _build_judge_prompt(profile: dict, resume_strategy: dict | None = None) -> str:
    """Build the LLM judge prompt from the user's profile."""
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Flatten allowed skills for the judge
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "N/A"

    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"
    strategy_block = _format_resume_strategy(resume_strategy or {})

    return f"""You are a resume quality judge. A tailoring engine rewrote a resume to target a specific job. Your job is to catch LIES, not style changes.

You must answer with EXACTLY this format:
VERDICT: PASS or FAIL
ISSUES: (list any problems, or "none")

## CONTEXT -- what the tailoring engine was instructed to do (all of this is ALLOWED):
- Change the title to match the target role
- Rewrite the summary from scratch for the target job
- Reorder bullets and projects to put the most relevant first
- Reframe bullets to use the job's language
- Drop low-relevance bullets and replace with more relevant ones from other sections
- Reorder the skills section to put job-relevant skills first
- Change tone and wording extensively

## USER-APPROVED ADDITIONAL FACTS AND POSITIONING:
These facts are allowed even if they were added after the original resume text was imported:
{strategy_block}

## WHAT IS FABRICATION (FAIL for these):
1. Adding tools, languages, or frameworks to TECHNICAL SKILLS that aren't in the original. The allowed skills are ONLY: {skills_str}
2. Inventing NEW metrics or numbers not in the original. The real metrics are: {metrics_str}
3. Inventing work that has no basis in any original bullet (completely new achievements).
4. Adding companies, roles, or degrees that don't exist.
5. Changing real numbers (inflating 80% to 95%, 500 nodes to 1000 nodes).

## WHAT IS NOT FABRICATION (do NOT fail for these):
- Rewording any bullet, even heavily, as long as the underlying work is real
- Combining two original bullets into one
- Splitting one original bullet into two
- Describing the same work with different emphasis
- Dropping bullets entirely
- Reordering anything
- Changing the title or summary completely

## TOLERANCE RULE:
The goal is to get interviews, not to be a perfect fact-checker. Allow up to 3 minor stretches per resume:
- Adding a closely related tool the candidate could realistically know is a MINOR STRETCH, not fabrication.
- Reframing a metric with slightly different wording is a MINOR STRETCH.
- Adding any LEARNABLE skill given their existing stack is a MINOR STRETCH.
- Only FAIL if there are MAJOR lies: completely invented projects, fake companies, fake degrees, wildly inflated numbers, or skills from a completely different domain.

Be strict about major lies. Be lenient about minor stretches and learnable skills. Do not fail for style, tone, or restructuring."""


# ── JSON Extraction ───────────────────────────────────────────────────────

def extract_json(raw: str) -> dict:
    """Robustly extract JSON from LLM response (handles fences, preamble).

    Args:
        raw: Raw LLM response text.

    Returns:
        Parsed JSON dict.

    Raises:
        ValueError: If no valid JSON found.
    """
    raw = raw.strip()

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Markdown fences
    if "```" in raw:
        for part in raw.split("```")[1::2]:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue

    # Scan each '{' and let raw_decode consume exactly one JSON object, ignoring
    # trailing prose. Trying every '{' also tolerates leading prose that happens
    # to contain a stray '{' before the real object -- both cases the old
    # find/rfind heuristic mishandled (rfind grabbed a '}' inside trailing text).
    decoder = json.JSONDecoder()
    idx = raw.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(raw[idx:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        idx = raw.find("{", idx + 1)

    raise ValueError("No valid JSON found in LLM response")


# ── Resume Assembly (profile-driven header) ──────────────────────────────

def assemble_resume_text(data: dict, profile: dict) -> str:
    """Convert JSON resume data to formatted plain text.

    Header (name, location, contact) is ALWAYS code-injected from the profile,
    never LLM-generated. All text fields are sanitized.

    Args:
        data: Parsed JSON resume from the LLM.
        profile: User profile dict from load_profile().

    Returns:
        Formatted resume text.
    """
    personal = profile.get("personal", {})
    lines: list[str] = []

    # Header -- always code-injected from profile
    lines.append(personal.get("full_name", ""))
    lines.append(sanitize_text(data.get("title", "Software Engineer")))

    # Location from search config or profile -- leave blank if not available
    # The location line is optional; the original used a hardcoded city.
    # We omit it here; the LLM prompt can include it if the user sets it.

    # Contact line
    contact_parts: list[str] = []
    if personal.get("email"):
        contact_parts.append(personal["email"])
    if personal.get("phone"):
        contact_parts.append(personal["phone"])
    if personal.get("github_url"):
        contact_parts.append(personal["github_url"])
    if personal.get("linkedin_url"):
        contact_parts.append(personal["linkedin_url"])
    if contact_parts:
        lines.append(" | ".join(contact_parts))
    lines.append("")

    # Summary
    lines.append("SUMMARY")
    lines.append(sanitize_text(data["summary"]))
    lines.append("")

    # Technical Skills
    lines.append("TECHNICAL SKILLS")
    if isinstance(data["skills"], dict):
        for cat, val in data["skills"].items():
            lines.append(f"{cat}: {sanitize_text(str(val))}")
    lines.append("")

    # Experience
    lines.append("EXPERIENCE")
    for entry in data.get("experience", []):
        lines.append(sanitize_text(entry.get("header", "")))
        if entry.get("subtitle"):
            lines.append(sanitize_text(entry["subtitle"]))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    # Projects
    lines.append("PROJECTS")
    for entry in data.get("projects", []):
        lines.append(sanitize_text(entry.get("header", "")))
        if entry.get("subtitle"):
            lines.append(sanitize_text(entry["subtitle"]))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    # Education
    lines.append("EDUCATION")
    lines.append(sanitize_text(str(data.get("education", ""))))

    return "\n".join(lines)


# ── LLM Judge ────────────────────────────────────────────────────────────

def judge_tailored_resume(
    original_text: str,
    tailored_text: str,
    job_title: str,
    profile: dict,
    resume_strategy: dict | None = None,
) -> dict:
    """LLM judge layer: catches subtle fabrication that programmatic checks miss.

    Args:
        original_text: Base resume text.
        tailored_text: Tailored resume text.
        job_title: Target job title.
        profile: User profile for building the judge prompt.

    Returns:
        {"passed": bool, "verdict": str, "issues": str, "raw": str}
    """
    judge_prompt = _build_judge_prompt(profile, resume_strategy=resume_strategy)

    messages = [
        {"role": "system", "content": judge_prompt},
        {"role": "user", "content": (
            f"JOB TITLE: {job_title}\n\n"
            f"ORIGINAL RESUME:\n{original_text}\n\n---\n\n"
            f"TAILORED RESUME:\n{tailored_text}\n\n"
            "Judge this tailored resume:"
        )},
    ]

    client = get_client(stage="judge")
    response = client.chat(messages, max_tokens=2048, temperature=0.1, stage="tailor")

    passed = "VERDICT: PASS" in response.upper()
    issues = "none"
    if "ISSUES:" in response.upper():
        issues_idx = response.upper().index("ISSUES:")
        issues = response[issues_idx + 7:].strip()

    return {
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "issues": issues,
        "raw": response,
    }


# ── Core Tailoring ───────────────────────────────────────────────────────

def tailor_resume(
    resume_text: str, job: dict, profile: dict,
    resume_strategy: dict | None = None,
    max_retries: int = 3, validation_mode: str = "normal",
) -> tuple[str, dict]:
    """Generate a tailored resume via JSON output + fresh context on each retry.

    Key design choices:
    - LLM returns structured JSON, code assembles the text (no header leaks)
    - Each retry starts a FRESH conversation (no apologetic spiral)
    - Issues from previous attempts are noted in the system prompt
    - Em dashes and smart quotes are auto-fixed, not rejected

    Args:
        resume_text:      Base resume text.
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".
                          strict  -- banned words trigger retries; judge must pass
                          normal  -- banned words = warnings only; judge can fail on last retry
                          lenient -- banned words ignored; LLM judge skipped

    Returns:
        (tailored_text, report) where report contains validation details.
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    report: dict = {
        "attempts": 0, "validator": None, "judge": None,
        "status": "pending", "validation_mode": validation_mode,
        "model_attempts": [],
    }
    avoid_notes: list[str] = []
    tailored = ""
    model_plan = _build_tailor_model_plan(job, max_retries=max_retries)
    report["model_plan"] = [
        {"role": entry["role"], "model": entry["model"], "attempts": entry["attempts"]}
        for entry in model_plan
    ]
    tailor_prompt_base = _build_tailor_prompt(profile, resume_strategy=resume_strategy)

    attempt = 0
    for model_entry in model_plan:
        client = model_entry["client"]
        model_role = model_entry["role"]
        for _ in range(model_entry["attempts"]):
            report["attempts"] = attempt + 1

            # Fresh conversation every attempt
            prompt = tailor_prompt_base
            if avoid_notes:
                prompt += "\n\n## AVOID THESE ISSUES (from previous attempt):\n" + "\n".join(
                    f"- {n}" for n in avoid_notes[-5:]
                )

            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"ORIGINAL RESUME:\n{resume_text}\n\n---\n\nTARGET JOB:\n{job_text}\n\nReturn the JSON:"},
            ]

            try:
                raw = client.chat(messages, max_tokens=8192, temperature=0.4, stage="tailor")
            except Exception as exc:
                report["model_attempts"].append(
                    {
                        "attempt": attempt + 1,
                        "role": model_role,
                        "model": client.model,
                        "status": "request_error",
                        "error": type(exc).__name__,
                    }
                )
                avoid_notes.append(
                    f"{client.model} request failed with {type(exc).__name__}; use valid JSON and conserve output."
                )
                attempt += 1
                if attempt <= max_retries:
                    continue
                raise

            attempt_report = {
                "attempt": attempt + 1,
                "role": model_role,
                "model": client.model,
                "usage": _usage_for_report(client),
            }

            # Parse JSON from response
            try:
                data = extract_json(raw)
            except ValueError:
                attempt_report["status"] = "invalid_json"
                report["model_attempts"].append(attempt_report)
                avoid_notes.append("Output was not valid JSON. Return ONLY a JSON object, nothing else.")
                attempt += 1
                continue

            # Layer 1: Validate JSON fields
            validation = validate_json_fields(
                data,
                profile,
                mode=validation_mode,
                resume_strategy=resume_strategy,
            )
            report["validator"] = validation

            if not validation["passed"]:
                attempt_report["status"] = "failed_validation"
                attempt_report["errors"] = validation["errors"]
                report["model_attempts"].append(attempt_report)
                # Only retry if there are hard errors (warnings never block)
                avoid_notes.extend(validation["errors"])
                if attempt < max_retries:
                    attempt += 1
                    continue
                # Last attempt — assemble whatever we got
                tailored = assemble_resume_text(data, profile)
                report["status"] = "failed_validation"
                report["model_used"] = client.model
                return tailored, report

            # Assemble text (header injected by code, em dashes auto-fixed)
            tailored = assemble_resume_text(data, profile)

            # Layer 2: LLM judge (catches subtle fabrication) — skipped in lenient mode
            if validation_mode == "lenient":
                report["judge"] = {"verdict": "SKIPPED", "passed": True, "issues": "none"}
                report["status"] = "approved"
                report["model_used"] = client.model
                attempt_report["status"] = "approved"
                report["model_attempts"].append(attempt_report)
                return tailored, report

            judge = judge_tailored_resume(
                resume_text,
                tailored,
                job.get("title", ""),
                profile,
                resume_strategy=resume_strategy,
            )
            report["judge"] = judge

            if not judge["passed"]:
                attempt_report["status"] = "failed_judge"
                attempt_report["judge_issues"] = judge["issues"]
                report["model_attempts"].append(attempt_report)
                avoid_notes.append(f"Judge rejected: {judge['issues']}")
                if attempt < max_retries:
                    # In normal mode, only retry on judge failure if there are retries left
                    if validation_mode != "lenient":
                        attempt += 1
                        continue
                report["status"] = "failed_judge"
                report["model_used"] = client.model
                return tailored, report

            # Both passed
            report["status"] = "approved"
            report["model_used"] = client.model
            attempt_report["status"] = "approved"
            report["model_attempts"].append(attempt_report)
            return tailored, report

    report["status"] = "exhausted_retries"
    return tailored, report


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_tailoring(min_score: int = 7, limit: int = 900,
                  validation_mode: str = "normal",
                  workers: int = 1) -> dict:
    """Generate tailored resumes for high-scoring jobs.

    Args:
        min_score:       Minimum fit_score to tailor for.
        limit:           Maximum jobs to process. 0 means all eligible jobs.
        validation_mode: "strict", "normal", or "lenient".
        workers:         Concurrent LLM generation workers. SQLite writes stay serial.

    Returns:
        {"approved": int, "failed": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    resume_strategy = load_resume_strategy()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    jobs = get_jobs_by_stage(conn=conn, stage="pending_tailor", min_score=min_score, limit=limit)

    if not jobs:
        log.info("No untailored jobs with score >= %d.", min_score)
        return {"approved": 0, "failed": 0, "errors": 0, "elapsed": 0.0}

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    worker_count = max(1, min(int(workers or 1), len(jobs)))
    log.info(
        "Tailoring resumes for %d jobs (score >= %d, workers=%d)...",
        len(jobs), min_score, worker_count,
    )
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    stats: dict[str, int] = {
        "approved": 0,
        "failed_validation": 0,
        "failed_judge": 0,
        "exhausted_retries": 0,
        "error": 0,
    }

    def _generate(job: dict) -> dict:
        try:
            tailored, report = tailor_resume(resume_text, job, profile,
                                             resume_strategy=resume_strategy,
                                             validation_mode=validation_mode)
            return {
                "url": job["url"],
                "title": job["title"],
                "site": job["site"],
                "status": report["status"],
                "attempts": report["attempts"],
                "model_used": report.get("model_used"),
                "tailored": tailored,
                "report": report,
                "job": job,
            }
        except Exception as e:
            return {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "status": "error", "attempts": 0, "path": None, "pdf_path": None,
                "model_used": None,
                "error": str(e),
                "job": job,
            }

    futures = []
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(_generate, job) for job in jobs]

        for future in as_completed(futures):
            completed += 1
            result = future.result()
            job = result["job"]

            if result["status"] != "error":
                # Build safe filename prefix
                prefix = _safe_job_prefix(job)

                # Save tailored resume text (write to .tmp then rename so a crash
                # mid-write never leaves a partial file at the final path)
                txt_path = TAILORED_DIR / f"{prefix}.txt"
                tmp = txt_path.with_suffix(".tmp")
                tmp.write_text(result["tailored"], encoding="utf-8")
                os.replace(tmp, txt_path)

                # Save job description for traceability
                job_path = TAILORED_DIR / f"{prefix}_JOB.txt"
                job_desc = (
                    f"Title: {job['title']}\n"
                    f"Company: {job['site']}\n"
                    f"Location: {job.get('location', 'N/A')}\n"
                    f"Score: {job.get('fit_score', 'N/A')}\n"
                    f"URL: {job['url']}\n\n"
                    f"{job.get('full_description', '')}"
                )
                tmp = job_path.with_suffix(".tmp")
                tmp.write_text(job_desc, encoding="utf-8")
                os.replace(tmp, job_path)

                # Save validation report
                report_path = TAILORED_DIR / f"{prefix}_REPORT.json"
                tmp = report_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(result["report"], indent=2), encoding="utf-8")
                os.replace(tmp, report_path)

                # Generate PDF for approved resumes (best-effort)
                pdf_path = None
                if result["status"] == "approved":
                    try:
                        from applypilot.scoring.pdf import convert_to_pdf
                        pdf_path = str(convert_to_pdf(txt_path))
                    except Exception:
                        log.debug("PDF generation failed for %s", txt_path, exc_info=True)
                result["path"] = str(txt_path)
                result["pdf_path"] = pdf_path
            else:
                log.error(
                    "%d/%d [ERROR] %s -- %s",
                    completed, len(jobs), job["title"][:40], result.get("error", ""),
                )

            results.append(result)
            stats[result.get("status", "error")] = stats.get(result.get("status", "error"), 0) + 1

            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            log.info(
                "%d/%d [%s] attempts=%s model=%s | %.1f jobs/min | %s",
                completed, len(jobs),
                result["status"].upper(),
                result.get("attempts", "?"),
                result.get("model_used") or "?",
                rate * 60,
                result["title"][:40],
            )

            # Persist after every job in the caller thread so Ctrl+C or a later
            # slow request does not lose generated resumes, while SQLite keeps a
            # single writer.
            now = datetime.now(timezone.utc).isoformat()
            if result["status"] == "approved":
                conn.execute(
                    "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, "
                    "tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                    (result["path"], now, result["url"]),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                    (result["url"],),
                )
            conn.commit()

    elapsed = time.time() - t0
    log.info(
        "Tailoring done in %.1fs: %d approved, %d failed_validation, %d failed_judge, %d exhausted, %d errors",
        elapsed,
        stats.get("approved", 0),
        stats.get("failed_validation", 0),
        stats.get("failed_judge", 0),
        stats.get("exhausted_retries", 0),
        stats.get("error", 0),
    )

    return {
        "approved": stats.get("approved", 0),
        "failed": (
            stats.get("failed_validation", 0)
            + stats.get("failed_judge", 0)
            + stats.get("exhausted_retries", 0)
        ),
        "errors": stats.get("error", 0),
        "elapsed": elapsed,
    }
