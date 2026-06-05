"""Resume and cover letter validation: banned words, fabrication detection, structural checks.

All validation is profile-driven -- no hardcoded personal data. The validator receives
a profile dict (from applypilot.config.load_profile()) and validates against the user's
actual skills, companies, projects, and school.

Validation modes
----------------
strict  -- banned words = hard errors that trigger retries (original behavior)
normal  -- banned words = warnings only; fabrication/structure = errors (default)
lenient -- banned words ignored; only fabrication and required structure checked
"""

import json
import re
import logging

log = logging.getLogger(__name__)


# â”€â”€ Universal Constants (not personal data) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

BANNED_WORDS: list[str] = [
    "passionate", "dedicated", "committed to",
    "utilizing", "utilize", "harnessing",
    "spearheaded", "spearhead", "orchestrated", "championed", "pioneered",
    "robust", "scalable solutions", "cutting-edge", "state-of-the-art", "best-in-class",
    "proven track record", "track record of success", "demonstrated ability",
    "strong communicator", "team player", "fast learner", "self-starter", "go-getter",
    "synergy", "cross-functional collaboration", "holistic",
    "transformative", "innovative solutions", "paradigm", "ecosystem",
    "proactive", "detail-oriented", "highly motivated",
    "seamless", "full lifecycle",
    "deep understanding", "extensive experience", "comprehensive knowledge",
    "thrives in", "excels at", "adept at", "well-versed in",
    "i am confident", "i believe", "i am excited",
    "plays a critical role", "instrumental in", "integral part of",
    "strong track record", "eager to", "eager",
    # Cover-letter-specific additions
    "this demonstrates", "this reflects", "i have experience with",
    "furthermore", "additionally", "moreover",
]

LLM_LEAK_PHRASES: list[str] = [
    "i am sorry", "i apologize", "i will try", "let me try",
    "i am at a loss", "i am truly sorry", "apologies for",
    "i keep fabricating", "i will have to admit", "one final attempt",
    "one last time", "if it fails again", "persistent errors",
    "i am having difficulty", "i made an error", "my mistake",
    "here is the corrected", "here is the revised", "here is the updated",
    "here is my", "below is the", "as requested",
    "note:", "disclaimer:", "important:",
    "i have rewritten", "i have removed", "i have fixed",
    "i have replaced", "i have updated", "i have corrected",
    "per your feedback", "based on your feedback", "as per the instructions",
    "the following resume", "the resume below",
    "the following cover letter", "the letter below",
]

# Known fabrication markers: completely unrelated tools/languages.
# Reasonable stretches (K8s, Terraform, Redis, Kafka etc.) are ALLOWED.
FABRICATION_WATCHLIST: set[str] = {
    # Languages with zero relation to the candidate's stack
    "c#", "c++", "golang", "rust", "ruby",
    "kotlin", "swift", "scala", "matlab",
    # Frameworks for wrong languages
    "spring", "django", "rails", "angular", "vue", "svelte",
    # Hard lies: certifications can't be stretched
    "certif", "certified", "pmp", "scrum master", "aws certified",
}

REQUIRED_SECTIONS: set[str] = {"SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"}


# â”€â”€ Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _build_skills_set(profile: dict) -> set[str]:
    """Build the set of allowed skills from the profile's skills_boundary."""
    boundary = profile.get("skills_boundary", {})
    allowed: set[str] = set()
    for category in boundary.values():
        if isinstance(category, list):
            allowed.update(s.lower().strip() for s in category)
        elif isinstance(category, set):
            allowed.update(s.lower().strip() for s in category)
    return allowed


def _is_allowed_skill_token(token: str, allowed_skills: set[str]) -> bool:
    """Return True when a watchlist term is explicitly allowed by profile."""
    token_l = token.lower().strip()
    return any(token_l == skill or token_l in skill for skill in allowed_skills)


def _required_experience_companies(
    profile: dict,
    resume_strategy: dict | None = None,
) -> set[str]:
    """Companies that must remain in the Experience section.

    Older roles may be compressed into projects on a one-page tailored resume.
    Current employers and strategy-configured companies must stay in Experience.
    """
    required: set[str] = set()
    if resume_strategy:
        required.update(resume_strategy.get("required_experience_companies", []) or [])
        for item in resume_strategy.get("protected_timeline", []) or []:
            company = str(item.get("company", "")).strip()
            date = str(item.get("date", "")).lower()
            if company and "present" in date:
                required.add(company)

    if not required:
        companies = profile.get("resume_facts", {}).get("preserved_companies", []) or []
        if companies:
            required.add(companies[0])
    return required


def _normalize_token_text(text: str) -> str:
    """Normalize text for loose date/company matching."""
    normalized = text.lower()
    month_aliases = {
        "january": "jan",
        "february": "feb",
        "march": "mar",
        "april": "apr",
        "june": "jun",
        "july": "jul",
        "august": "aug",
        "september": "sep",
        "sept": "sep",
        "october": "oct",
        "november": "nov",
        "december": "dec",
    }
    for full, short in month_aliases.items():
        normalized = re.sub(rf"\b{full}\b", short, normalized)
    return re.sub(r"[^a-z0-9]+", "", normalized)


def sanitize_text(text: str) -> str:
    """Auto-fix common LLM output issues instead of rejecting."""
    text = text.replace(" \u2014 ", ", ").replace("\u2014", ", ")   # em dash -> comma
    text = text.replace("\u2013", "-")    # en dash -> hyphen
    text = text.replace("\u201c", '"').replace("\u201d", '"')   # smart double quotes
    text = text.replace("\u2018", "'").replace("\u2019", "'")   # smart single quotes
    return text.strip()


# â”€â”€ JSON Field Validation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def validate_json_fields(
    data: dict,
    profile: dict,
    mode: str = "normal",
    resume_strategy: dict | None = None,
) -> dict:
    """Validate individual JSON fields from an LLM-generated tailored resume.

    Args:
        data:    Parsed JSON from the LLM (title, summary, skills, experience, projects, education).
        profile: User profile dict from load_profile().
        mode:    Validation strictness â€” "strict", "normal", or "lenient".
                 strict  â†’ banned words are errors (trigger retries)
                 normal  â†’ banned words are warnings (no retry)
                 lenient â†’ banned words ignored entirely

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Required keys. Projects are only required when the profile has real preserved projects.
    resume_facts = profile.get("resume_facts", {})
    required_keys = ["title", "summary", "skills", "experience", "education"]
    strategy_projects = []
    if resume_strategy:
        strategy_projects = resume_strategy.get("required_projects", []) or []
    if resume_facts.get("preserved_projects") or strategy_projects:
        required_keys.append("projects")
    else:
        data.setdefault("projects", [])

    for key in required_keys:
        if key not in data or not data[key]:
            errors.append(f"Missing required field: {key}")
    if errors:
        return {"passed": False, "errors": errors, "warnings": warnings}

    # Collect all text for bulk checks
    all_text_parts: list[str] = [data["summary"]]

    # Skills: check for fabrication (always enforced)
    allowed_skills = _build_skills_set(profile)
    if isinstance(data["skills"], dict):
        skills_text = " ".join(str(v) for v in data["skills"].values()).lower()
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in skills_text and not _is_allowed_skill_token(fake, allowed_skills):
                errors.append(f"Fabricated skill: '{fake}'")

    # Experience: current/required companies must be present. Older roles may
    # be compressed into projects or omitted for one-page fit.
    resume_facts = profile.get("resume_facts", {})
    preserved_companies = resume_facts.get("preserved_companies", [])
    required_companies = _required_experience_companies(profile, resume_strategy)
    protected_timeline = []
    if resume_strategy:
        protected_timeline = resume_strategy.get("protected_timeline", []) or []

    if isinstance(data["experience"], list):
        for company in required_companies:
            has_company = any(
                company.lower() in str(e.get("header", "")).lower()
                for e in data["experience"]
            )
            if not has_company:
                errors.append(f"Company '{company}' missing from experience")
        all_json_text = json.dumps(data, ensure_ascii=False).lower()
        for company in preserved_companies:
            if company not in required_companies and company.lower() not in all_json_text:
                warnings.append(f"Company '{company}' omitted from one-page resume")
        for item in protected_timeline:
            company = str(item.get("company", ""))
            expected_date = str(item.get("date", ""))
            if not company or not expected_date:
                continue
            company_norm = _normalize_token_text(company)
            expected_norm = _normalize_token_text(expected_date)
            matches = [
                e for e in data["experience"]
                if company_norm in _normalize_token_text(str(e.get("header", "")))
            ]
            if matches and not any(
                expected_norm in _normalize_token_text(
                    f"{e.get('header', '')} {e.get('subtitle', '')}"
                )
                for e in matches
            ):
                errors.append(f"Date for '{company}' changed from '{expected_date}'")
        for entry in data["experience"]:
            for b in entry.get("bullets", []):
                all_text_parts.append(b)

    # Projects: collect bullets. If strategy projects are configured, blank
    # projects are a quality failure, not a valid "minimal" resume.
    if isinstance(data["projects"], list):
        for entry in data["projects"]:
            for b in entry.get("bullets", []):
                all_text_parts.append(b)
    if strategy_projects and not data.get("projects"):
        errors.append("Projects section is empty despite configured required projects")

    # Education: preserved school must be present (always enforced)
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school:
        edu = str(data.get("education", ""))
        if preserved_school.lower() not in edu.lower():
            errors.append(f"Education '{preserved_school}' missing")

    # Bulk text checks
    all_text = " ".join(all_text_parts).lower()

    # LLM self-talk is always an error regardless of mode (indicates broken output)
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in all_text]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # Banned filler words â€” severity depends on mode
    if mode != "lenient":
        found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", all_text)]
        if found_banned:
            msg = f"Banned words: {', '.join(found_banned[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}

# â”€â”€ Full Resume Text Validation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def validate_tailored_resume(text: str, profile: dict, original_text: str = "") -> dict:
    """Programmatic validation of a tailored resume against the user's profile.

    Args:
        text: The tailored resume text to validate.
        profile: User profile dict from load_profile().
        original_text: The original base resume text (for fabrication comparison).

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    personal = profile.get("personal", {})
    resume_facts = profile.get("resume_facts", {})

    # 1. Check required sections exist (flexible matching)
    section_variants: dict[str, list[str]] = {
        "SUMMARY": ["summary", "professional summary", "profile"],
        "TECHNICAL SKILLS": ["technical skills", "skills", "tech stack", "core skills", "technologies"],
        "EXPERIENCE": ["experience", "work experience", "professional experience"],
        "PROJECTS": ["projects", "personal projects", "key projects", "selected projects"],
        "EDUCATION": ["education", "academic background"],
    }
    for section, variants in section_variants.items():
        if not any(v in text_lower for v in variants):
            errors.append(f"Missing required section: {section} (or variant)")

    # 2. Check name preserved (warn, don't error -- we can inject it)
    full_name = personal.get("full_name", "")
    if full_name and full_name.lower() not in text_lower:
        warnings.append(f"Name '{full_name}' missing -- will be injected")

    # 3. Check current/required companies preserved
    for company in _required_experience_companies(profile):
        if company.lower() not in text_lower:
            errors.append(f"Company '{company}' missing -- cannot remove real experience")

    # 4. Check projects preserved
    for project in resume_facts.get("preserved_projects", []):
        if project.lower() not in text_lower:
            warnings.append(f"Project '{project}' not found -- may have been renamed")

    # 5. Check school preserved
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school and preserved_school.lower() not in text_lower:
        errors.append(f"Education '{preserved_school}' missing")

    # 6. Check contact info preserved (warn, don't error -- we can inject)
    email = personal.get("email", "")
    phone = personal.get("phone", "")
    if email and email.lower() not in text_lower:
        warnings.append("Email missing -- will be injected")
    if phone and phone not in text:
        warnings.append("Phone missing -- will be injected")

    # 7. Scan TECHNICAL SKILLS section for fabricated tools
    skills_start = text_lower.find("technical skills")
    skills_end = text_lower.find("experience", skills_start) if skills_start != -1 else -1
    if skills_start != -1 and skills_end != -1:
        skills_block = text_lower[skills_start:skills_end]
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in skills_block and not _is_allowed_skill_token(fake, _build_skills_set(profile)):
                errors.append(f"FABRICATED SKILL in Technical Skills: '{fake}'")

    # 8. Scan full document for fabrication watchlist items not in original
    if original_text:
        original_lower = original_text.lower()
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in text_lower and fake not in original_lower:
                warnings.append(f"New tool/skill appeared: '{fake}' (not in original)")

    # 9. Em dashes (should be auto-fixed by sanitize_text, but safety net)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 10. Banned words (word-boundary matching)
    found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
    if found_banned:
        errors.append(f"Banned words: {', '.join(found_banned[:5])}")

    # 11. LLM self-talk leak detection
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 12. Duplicate section detection
    for section_name in ["summary", "experience", "education", "projects"]:
        count = text_lower.count(f"\n{section_name}\n") + text_lower.count(f"\n{section_name} \n")
        if text_lower.startswith(f"{section_name}\n"):
            count += 1
        if count > 1:
            errors.append(f"Section '{section_name}' appears {count} times.")

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# â”€â”€ Cover Letter Validation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def validate_cover_letter(text: str, mode: str = "normal") -> dict:
    """Programmatic validation of a cover letter.

    Args:
        text: The cover letter text to validate.
        mode: Validation strictness â€” "strict", "normal", or "lenient".
              strict  â†’ banned words are errors (trigger retries); word limit enforced
              normal  â†’ banned words are warnings; word limit is soft (+25 words)
              lenient â†’ banned words ignored; word count not checked

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    # 1. Em dashes â€” always an error (sanitize_text should have caught these)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 2. Banned words â€” severity depends on mode
    if mode != "lenient":
        found = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
        if found:
            msg = f"Banned words: {', '.join(found[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    # 3. Word count
    words = len(text.split())
    if mode == "strict" and words > 250:
        errors.append(f"Too long ({words} words). Max 250.")
    elif mode == "normal" and words > 275:
        warnings.append(f"Long ({words} words). Target 250.")
    # lenient: no word count check

    # 4. LLM self-talk â€” always an error regardless of mode
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 5. Must start with "Dear" â€” always checked (preamble should have been stripped)
    stripped = text.strip()
    if not stripped.lower().startswith("dear"):
        errors.append("Must start with 'Dear Hiring Manager,'")

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}
