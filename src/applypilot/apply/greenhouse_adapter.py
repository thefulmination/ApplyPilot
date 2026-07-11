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

_GH_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
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


def _default_fetch(url: str) -> dict:
    resp = httpx.get(url, headers={"Accept": "application/json"}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_questions(board_token, job_id, *, fetch=None) -> list[dict]:
    """Return the job's application ``questions`` list from the public API.

    ``fetch`` is an injectable ``url -> parsed-json-dict`` callable (defaults to
    an unauthenticated httpx GET).
    """
    fetch = fetch or _default_fetch
    data = fetch(_QUESTIONS_API.format(board=board_token, job_id=job_id)) or {}
    return list(data.get("questions") or [])


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


def build_answer_plan(questions, *, profile, resume_text, corpus=None,
                      answer_fn=None, job=None, budget=None) -> AnswerPlan:
    """Compute a complete, deterministic submission plan for a Greenhouse job.

    Identity/phone/location and standard selects (work-auth, demographic-decline)
    are mapped from the profile with no model call. Only genuine ``textarea``
    free-text goes to ``answer_fn`` (defaults to the verifier-gated answerer), and
    an unverified free-text answer is dropped, not submitted. Any required field
    the adapter can't confidently fill is recorded in ``unmapped_required`` and
    makes ``ready`` False -- the caller escalates or skips instead of faking it.
    """
    default_answerer = answer_fn is None
    if default_answerer:
        from applypilot.apply.answerer import answer_question_bounded
        answer_fn = answer_question_bounded
        from applypilot.apply.phase_budget import PhaseBudgetManager
        budget = budget or PhaseBudgetManager()

    personal = (profile or {}).get("personal", {})
    work_auth = (profile or {}).get("work_authorization", {})
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
                try:
                    res = answer_fn(
                        label, job=job or {}, profile=profile, resume_text=resume_text,
                        corpus=corpus, kind="open",
                        **({"budget": budget} if default_answerer else {}),
                    )
                except Exception as exc:
                    from applypilot.apply.phase_budget import PhaseBudgetExceeded
                    if not isinstance(exc, PhaseBudgetExceeded):
                        raise
                    continue
                if getattr(res, "verified", False):
                    fields[name] = res.text
                    free_text[name] = res.text
                    mapped = True
                continue  # unverified -> never submit; leave unmapped

            if ftype == "multi_value_single_select":
                val = None
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

            # multi_value_multi_select / unknown types: not handled in this slice.

        if required and not mapped:
            unmapped.append(label)

    ready = bool(fields.get("first_name") and fields.get("email")) and not unmapped
    return AnswerPlan(fields=fields, resume_field=resume_field, free_text=free_text,
                      unmapped_required=unmapped, ready=ready)
