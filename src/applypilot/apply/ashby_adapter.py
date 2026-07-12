"""Deterministic Ashby application-form adapter."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import urlparse

from bs4 import BeautifulSoup


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def adapter_enabled() -> bool:
    return _flag("APPLYPILOT_ASHBY_ADAPTER")


def submit_enabled() -> bool:
    return adapter_enabled() and _flag("APPLYPILOT_ASHBY_ADAPTER_SUBMIT")


def parse_ashby_url(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlparse(url)
    except (TypeError, ValueError):
        return None
    if (parsed.hostname or "").lower() != "jobs.ashbyhq.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


@dataclass(frozen=True)
class AshbyField:
    title: str
    path: str
    kind: str
    required: bool
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class AshbyAnswerPlan:
    fields: tuple[AshbyField, ...]
    values: dict[str, object]
    resume_field: str | None
    unmapped_required: list[str]
    ready: bool


def extract_form_definition(html: str) -> list[AshbyField]:
    script = next(
        (node.string or node.get_text() for node in BeautifulSoup(html or "", "html.parser").find_all("script")
         if "window.__appData" in (node.string or node.get_text() or "")),
        None,
    )
    if not script:
        return []
    marker = script.index("window.__appData")
    start = script.index("{", marker)
    data, _ = json.JSONDecoder().raw_decode(script[start:])
    definition = data["posting"]["applicationForm"]["formDefinition"]
    result: list[AshbyField] = []
    for section in definition.get("sections") or []:
        for entry in section.get("fields") or []:
            field = entry.get("field") or {}
            title, path, kind = field.get("title"), field.get("path"), field.get("type")
            if not title or not path or not kind:
                continue
            raw_options = field.get("selectableValues") or field.get("values") or []
            options = tuple(str(option.get("label")) for option in raw_options if option.get("label"))
            result.append(AshbyField(title, path, kind, bool(entry.get("isRequired")), options))
    return result


def _bool(value) -> bool | None:
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "y", "1"}:
        return True
    if normalized in {"false", "no", "n", "0"}:
        return False
    return None


def _select(options: tuple[str, ...], wanted: bool) -> str | None:
    prefix = "yes" if wanted else "no"
    return next((option for option in options if option.strip().lower().startswith(prefix)), None)


def build_answer_plan(fields, *, profile, resume_text, answer_fn=None, job=None, budget=None) -> AshbyAnswerPlan:
    default_answerer = answer_fn is None
    if default_answerer:
        from applypilot.apply.answerer import answer_question_bounded
        answer_fn = answer_question_bounded
        from applypilot.apply.phase_budget import PhaseBudgetManager
        budget = budget or PhaseBudgetManager()
    personal = (profile or {}).get("personal", {})
    auth = (profile or {}).get("work_authorization", {})
    values: dict[str, object] = {}
    unmapped: list[str] = []
    resume_field = None

    for field in fields:
        low = field.title.lower()
        value = None
        known = True
        if field.path == "_systemfield_name":
            value = personal.get("full_name")
        elif field.path == "_systemfield_email":
            value = personal.get("email")
        elif field.path == "_systemfield_location":
            value = ", ".join(filter(None, (personal.get("city"), personal.get("province_state"), personal.get("country"))))
        elif field.path == "_systemfield_resume" or field.kind == "File":
            resume_field = field.path
            value = "__resume__"
        elif "phone" in low:
            value = personal.get("phone")
        elif "linkedin" in low:
            value = personal.get("linkedin") or personal.get("linkedin_url")
        elif "preferred first" in low:
            value = (personal.get("full_name") or "").split()[0] if personal.get("full_name") else None
        elif "preferred last" in low:
            value = (personal.get("full_name") or "").split()[-1] if personal.get("full_name") else None
        elif "address" in low:
            value = personal.get("address")
        elif field.kind == "Boolean" and ("authorized" in low or "eligible to work" in low):
            value = _bool(auth.get("legally_authorized_to_work"))
        elif field.kind == "ValueSelect" and "sponsor" in low:
            sponsorship = _bool(auth.get("require_sponsorship"))
            value = _select(field.options, sponsorship) if sponsorship is not None else None
        elif field.kind in {"LongText", "String"}:
            try:
                answer = answer_fn(field.title, job=job or {}, profile=profile,
                                   resume_text=resume_text, kind="open",
                                   **({"budget": budget} if default_answerer else {}))
                value = answer.text if getattr(answer, "verified", False) else None
            except Exception as exc:
                from applypilot.apply.phase_budget import PhaseBudgetExceeded
                if not isinstance(exc, PhaseBudgetExceeded):
                    raise
                value = None
        else:
            known = False
        if value not in (None, ""):
            values[field.path] = value
        elif field.required:
            unmapped.append(field.title)
        elif not known:
            continue

    ready = bool(values.get("_systemfield_name") and values.get("_systemfield_email")) and not unmapped
    return AshbyAnswerPlan(tuple(fields), values, resume_field, unmapped, ready)


def _confirmed(page, *, inbox_confirmed=False) -> bool:
    if inbox_confirmed:
        return True
    try:
        text = (page.content() or "").lower()
        if any(marker in text for marker in ("thank you for applying", "application was submitted",
                                             "application has been submitted")):
            return True
    except Exception:
        pass
    try:
        return any(part in {"confirmation", "submitted", "thank-you"}
                   for part in urlparse(str(page.url)).path.lower().split("/"))
    except Exception:
        return False


def apply_ashby(job_url, *, page, profile, resume_text, resume_path,
                answer_fn=None, inbox_confirmation_fn=None, dry_run=True) -> dict:
    parsed = parse_ashby_url(job_url)
    if not parsed:
        return {"route": "not_ashby"}
    fields = extract_form_definition(page.content())
    plan = build_answer_plan(fields, profile=profile, resume_text=resume_text,
                             answer_fn=answer_fn, job={"site": parsed[0]})
    if not plan.ready:
        return {"route": "exception", "ready": False, "unmapped": plan.unmapped_required, "plan": plan}
    for field in plan.fields:
        if field.path not in plan.values:
            continue
        locator = page.get_by_label(field.title, exact=True)
        value = plan.values[field.path]
        if value == "__resume__":
            if resume_path:
                locator.set_input_files(resume_path)
        elif field.kind == "ValueSelect":
            locator.select_option(label=str(value))
        elif field.kind == "Boolean":
            if value:
                locator.check()
        else:
            locator.fill(str(value))
    result = {"route": "deterministic", "ready": True, "plan": plan,
              "submit_attempted": False}
    if dry_run:
        return result
    result["submit_attempted"] = True
    try:
        page.get_by_role("button", name="Submit Application", exact=True).click()
    except Exception:
        result["status"] = "failed:no_confirmation"
        return result
    inbox = False
    if inbox_confirmation_fn:
        try:
            inbox = bool(inbox_confirmation_fn(board=parsed[0], job_id=parsed[1]))
        except Exception:
            inbox = False
    result["status"] = "applied" if _confirmed(page, inbox_confirmed=inbox) else "failed:no_confirmation"
    return result
