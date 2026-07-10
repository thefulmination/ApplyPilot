"""Deterministic Greenhouse apply adapter.

Reads a Greenhouse job's application questions from the public Job Board API,
then builds a complete "answer plan" deterministically: standard identity/phone/
location fields come straight from the profile, demographic selects decline,
work-authorization selects map from the profile, and ONLY genuine free-text
questions go to the cheap-model answerer (verifier-gated). A required field the
adapter cannot confidently map is never faked -- it lands in ``unmapped_required``
so the caller escalates or skips rather than submitting a wrong answer.

Reading questions is an unauthenticated public GET. Submitting an application as
an applicant is a separate concern (hosted-form POST / DOM) handled elsewhere;
this module's job is to compute WHAT to submit.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

_GH_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
_GH_SHORT_HOSTS = {"grnh.se"}
_QUESTIONS_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=true"


def parse_greenhouse_url(url: str) -> tuple[str, str] | None:
    """Return (board_token, job_id) for a Greenhouse job URL, else None."""
    try:
        p = urllib.parse.urlparse(url)
    except (ValueError, TypeError):
        return None
    if (p.hostname or "").lower() not in _GH_HOSTS:
        return None
    parts = [x for x in p.path.split("/") if x]
    if "jobs" not in parts:
        return None
    i = parts.index("jobs")
    if i < 1 or i + 1 >= len(parts):
        return None
    token = parts[i - 1]
    job_id = re.sub(r"\D", "", parts[i + 1]) or None
    if not token or not job_id:
        return None
    return (token, job_id)


def resolve_greenhouse_url(url: str, *, resolve=None) -> str | None:
    """Return a canonical hosted Greenhouse URL, following ``grnh.se`` once.

    Direct hosted URLs are returned without network I/O. A short URL is useful
    only when its final redirect is still a parseable Greenhouse job.
    """
    if parse_greenhouse_url(url):
        return url
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except (TypeError, ValueError):
        return None
    if host not in _GH_SHORT_HOSTS:
        return None
    if resolve is None:
        def resolve(value):
            response = httpx.get(value, follow_redirects=True, timeout=20)
            response.raise_for_status()
            return str(response.url)
    try:
        final_url = str(resolve(url) or "")
    except Exception:
        return None
    return final_url if parse_greenhouse_url(final_url) else None


def _default_fetch(url: str) -> dict:
    resp = httpx.get(url, headers={"Accept": "application/json"}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_job(board_token, job_id, *, fetch=None) -> dict:
    """Return one public Greenhouse job payload, including form questions."""
    fetch = fetch or _default_fetch
    return fetch(_QUESTIONS_API.format(board=board_token, job_id=job_id)) or {}


def fetch_questions(board_token, job_id, *, fetch=None) -> list[dict]:
    """Return the job's application ``questions`` list from the public API.

    ``fetch`` is an injectable ``url -> parsed-json-dict`` callable (defaults to
    an unauthenticated httpx GET).
    """
    data = fetch_job(board_token, job_id, fetch=fetch)
    return list(data.get("questions") or [])


def job_context_from_payload(data: dict, *, board: str) -> dict:
    return {
        "site": board,
        "company": board,
        "title": str(data.get("title") or ""),
        "description": BeautifulSoup(
            str(data.get("content") or ""), "html.parser",
        ).get_text("\n", strip=True),
    }


@dataclass
class AnswerPlan:
    fields: dict            # submit field name -> value (identity, phone, selects, free-text)
    resume_field: str | None  # field expecting the resume file (usually "resume")
    free_text: dict         # subset of fields produced by the answerer: name -> text
    unmapped_required: list  # labels of required questions we would NOT fake
    ready: bool             # identity present AND nothing required left unmapped


# Label classifiers for select questions.
_DEMOGRAPHIC = ("gender", "race", "ethnic", "veteran", "disab", "hispanic",
                "lgbt", "sexual orientation", "pronoun")
_WORK_AUTH = ("authorized to work", "legally authorized", "work authorization",
              "eligible to work")
_SPONSOR = ("sponsorship", "visa sponsor", "require sponsorship", "need sponsorship")
_DECLINE = ("decline", "prefer not", "don't wish", "do not wish", "not to answer",
            "not to disclose", "not to identify", "not wish to")

_US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "Washington, D.C.",
}


def _name_parts(full: str) -> tuple[str, str]:
    toks = (full or "").split()
    return (toks[0] if toks else "", toks[-1] if len(toks) > 1 else "")


def _pick_value(values, pred):
    for v in values or []:
        if pred((v.get("label") or "").strip().lower()):
            return v.get("value")
    return None


def _has(label: str, needles) -> bool:
    return any(n in label for n in needles)


def _profile_text_answer(
    low: str, *, personal, compensation, experience, availability, first, last,
):
    label = " ".join(low.split()).strip(" :?*")
    if "desired salary" in label or "salary expectation" in label:
        value = compensation.get("salary_expectation") or compensation.get("salary_range_min")
        return str(value) if value not in (None, "") else None
    if "legal first name" in label:
        return first or None
    if "legal last name" in label:
        return last or None
    if label in {"address", "address line 1", "street address", "mailing address"}:
        return personal.get("address")
    if label in {"city", "current city"}:
        return personal.get("city")
    if "highest level of education" in label:
        return experience.get("education_level")
    if label in {"years of work experience", "total years of experience"}:
        value = experience.get("years_of_experience_total")
        return str(value) if value not in (None, "") else None
    if any(phrase in label for phrase in (
        "how did you hear", "how did you learn", "how did you first learn",
    )):
        return "Online job board"
    if "preferred start date" in label:
        return availability.get("earliest_start_date")
    return None


def _profile_select_value(
    low: str, values, *, personal, work_auth, experience, compensation, job,
):
    label = " ".join(low.split()).strip(" :?*")
    if "resident of the following states" in label:
        province = str(personal.get("province_state") or "").strip()
        state_code = province.upper()
        if len(state_code) != 2:
            state_code = next(
                (code for code, name in _US_STATE_NAMES.items() if name.lower() == province.lower()),
                "",
            )
        allowed = bool(state_code and re.search(rf"\b{re.escape(state_code.lower())}\b", label))
        answer = "yes" if allowed else "no"
        return _pick_value(values, lambda option: option == answer)
    state = str(personal.get("province_state") or "").strip()
    if "state" in label and state:
        candidates = {state.lower(), _US_STATE_NAMES.get(state.upper(), state).lower()}
        return _pick_value(values, lambda option: option in candidates)
    if label in {"country", "country of residence"}:
        country = str(personal.get("country") or "").strip().lower()
        aliases = {country}
        if country in {"us", "usa", "united states", "united states of america"}:
            aliases.update({"us", "usa", "united states", "united states of america"})
        return _pick_value(values, lambda option: option in aliases)
    if label == "address type" and personal.get("address"):
        return _pick_value(values, lambda option: option == "home")
    if any(phrase in label for phrase in (
        "privacy acknowledgement", "privacy policy", "data protection notice",
    )):
        return _pick_value(
            values,
            lambda option: "acknowledge" in option or option == "confirm",
        )
    if "declare" in label and "particulars" in label and "true" in label:
        return _pick_value(values, lambda option: option == "yes")
    if "documents" in label and "employment eligibility" in label:
        authorized = str(work_auth.get("legally_authorized_to_work") or "").lower()
        if authorized in {"yes", "true", "y"}:
            return _pick_value(values, lambda option: option == "yes")
    if label.startswith("are you located in "):
        city = str(personal.get("city") or "").strip().lower()
        province = str(personal.get("province_state") or "").strip()
        state_code = province.upper()
        if len(state_code) != 2:
            state_code = next(
                (code for code, name in _US_STATE_NAMES.items() if name.lower() == province.lower()),
                "",
            )
        located = bool(city and city in label and state_code and re.search(
            rf"\b{re.escape(state_code.lower())}\b", label,
        ))
        answer = "yes" if located else "no"
        return _pick_value(values, lambda option: option == answer)
    if "leadership/management experience" in label:
        title = str(experience.get("current_title") or "").lower()
        has_leadership = any(token in title for token in (
            "chief", "coo", "ceo", "president", "director", "manager", "head", "vp",
        ))
        answer = "yes" if has_leadership else "no"
        return _pick_value(values, lambda option: option == answer)
    if "compensation range match your expectation" in label:
        description = str((job or {}).get("description") or "")
        match = re.search(
            r"\$\s*([\d,]+)\s*(?:-|\u2013|\u2014|to)\s*\$?\s*([\d,]+)",
            description,
            re.I,
        )
        try:
            expected_min = float(str(compensation.get("salary_range_min") or "").replace(",", ""))
            expected_max = float(str(compensation.get("salary_range_max") or "").replace(",", ""))
            job_min = float(match.group(1).replace(",", "")) if match else 0
            job_max = float(match.group(2).replace(",", "")) if match else 0
        except (TypeError, ValueError):
            return None
        if expected_min and expected_max and job_min and job_max:
            overlaps = max(expected_min, job_min) <= min(expected_max, job_max)
            answer = "yes" if overlaps else "no"
            return _pick_value(values, lambda option: option == answer)
    if "location" in label and "closest" in label:
        city = str(personal.get("city") or "").strip()
        province = str(personal.get("province_state") or "").strip()
        state_code = province.upper()
        if len(state_code) != 2:
            state_code = next(
                (code for code, name in _US_STATE_NAMES.items() if name.lower() == province.lower()),
                "",
            )
        if city and state_code:
            exact = f"{state_code} | {city}".lower()
            return _pick_value(values, lambda option: option == exact)
    return None


def build_answer_plan(questions, *, profile, resume_text, corpus=None,
                      answer_fn=None, job=None) -> AnswerPlan:
    """Compute a complete, deterministic submission plan for a Greenhouse job.

    Identity/phone/location and standard selects (work-auth, demographic-decline)
    are mapped from the profile with no model call. Only genuine ``textarea``
    free-text goes to ``answer_fn`` (defaults to the verifier-gated answerer), and
    an unverified free-text answer is dropped, not submitted. Any required field
    the adapter can't confidently fill is recorded in ``unmapped_required`` and
    makes ``ready`` False -- the caller escalates or skips instead of faking it.
    """
    if answer_fn is None:
        from applypilot.apply.answerer import answer_question
        answer_fn = answer_question

    personal = (profile or {}).get("personal", {})
    work_auth = (profile or {}).get("work_authorization", {})
    compensation = (profile or {}).get("compensation", {})
    experience = (profile or {}).get("experience", {})
    availability = (profile or {}).get("availability", {})
    first, last = _name_parts(personal.get("full_name", ""))

    fields: dict = {}
    free_text: dict = {}
    resume_field: str | None = None
    unmapped: list = []

    for q in questions or []:
        required = bool(q.get("required"))
        label = q.get("label", "") or ""
        low = label.lower()
        mapped = False

        for f in q.get("fields", []) or []:
            name = f.get("name", "")
            ftype = f.get("type", "")
            values = f.get("values") or []

            if ftype == "input_hidden":
                mapped = True
                continue

            if ftype == "input_file":
                if name == "resume":
                    resume_field = name
                    mapped = True
                continue  # cover_letter / other files: optional -> skip

            if name == "first_name" and first:
                fields[name] = first
                mapped = True
                continue
            if name == "last_name" and last:
                fields[name] = last
                mapped = True
                continue
            if name == "email" and personal.get("email"):
                fields[name] = personal["email"]
                mapped = True
                continue
            if name == "phone" and personal.get("phone"):
                fields[name] = personal["phone"]
                mapped = True
                continue
            if name == "location":
                loc = ", ".join(
                    x for x in (personal.get("city"), personal.get("province_state"),
                                personal.get("country")) if x
                )
                if loc:
                    fields[name] = loc
                    mapped = True
                continue

            # Custom Greenhouse questions often use opaque names such as
            # question_123 even when the answer already exists in the profile.
            # Map only high-confidence labels; ambiguous questions still fall
            # through to the answerer or unmapped-required guard.
            if ftype == "input_text":
                value = _profile_text_answer(
                    low,
                    personal=personal,
                    compensation=compensation,
                    experience=experience,
                    availability=availability,
                    first=first,
                    last=last,
                )
                if "preferred" in low and "name" in low:
                    value = personal.get("preferred_name") or first
                elif "linkedin" in low:
                    value = personal.get("linkedin_url")
                elif "github" in low:
                    value = personal.get("github_url")
                elif "portfolio" in low:
                    value = personal.get("portfolio_url")
                elif "website" in low:
                    value = personal.get("website_url")
                elif "postal" in low or "zip code" in low:
                    value = personal.get("postal_code")
                if value:
                    fields[name] = str(value)
                    mapped = True
                continue

            profile_text = _profile_text_answer(
                low,
                personal=personal,
                compensation=compensation,
                experience=experience,
                availability=availability,
                first=first,
                last=last,
            )
            if ftype == "textarea" and profile_text:
                fields[name] = str(profile_text)
                mapped = True
                continue

            # Greenhouse offers resume/cover-letter as a paste TEXTAREA too
            # (name resume_text / cover_letter_text). These are documents, not
            # questions -- fill the resume verbatim, never send them to the
            # answerer (which would fabricate a resume).
            if name == "resume_text":
                if resume_text:
                    fields[name] = resume_text
                mapped = True
                continue
            if name == "cover_letter_text":
                mapped = True  # optional; leave empty rather than fabricate a letter
                continue

            if ftype == "textarea":
                res = answer_fn(label, job=job or {}, profile=profile,
                                resume_text=resume_text, corpus=corpus, kind="open")
                if getattr(res, "verified", False):
                    fields[name] = res.text
                    free_text[name] = res.text
                    mapped = True
                continue  # unverified -> never submit; leave unmapped

            if ftype == "multi_value_single_select":
                val = _profile_select_value(
                    low,
                    values,
                    personal=personal,
                    work_auth=work_auth,
                    experience=experience,
                    compensation=compensation,
                    job=job,
                )
                if _has(low, _DEMOGRAPHIC):
                    val = _pick_value(values, lambda label_l: _has(label_l, _DECLINE))
                elif _has(low, _SPONSOR):
                    if str(work_auth.get("require_sponsorship", "")).strip().lower() in ("no", "false", "n"):
                        val = _pick_value(values, lambda label_l: label_l == "no")
                elif _has(low, _WORK_AUTH):
                    if str(work_auth.get("legally_authorized_to_work", "")).strip().lower() in ("yes", "true", "y"):
                        val = _pick_value(values, lambda label_l: label_l == "yes")
                if val is not None:
                    fields[name] = val
                    mapped = True
                continue

            if ftype == "multi_value_multi_select":
                val = _profile_select_value(
                    low,
                    values,
                    personal=personal,
                    work_auth=work_auth,
                    experience=experience,
                    compensation=compensation,
                    job=job,
                )
                if val is not None:
                    fields[name] = val
                    mapped = True
                continue

            # multi_value_multi_select / unknown types: not handled in this slice.

        if required and not mapped:
            unmapped.append(label)

    ready = bool(fields.get("first_name") and fields.get("email")) and not unmapped
    return AnswerPlan(fields=fields, resume_field=resume_field, free_text=free_text,
                      unmapped_required=unmapped, ready=ready)
