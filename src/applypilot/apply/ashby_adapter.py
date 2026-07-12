"""Deterministic Ashby form discovery, planning, and browser actions."""

from __future__ import annotations

import os
import urllib.parse

from applypilot.apply.greenhouse_adapter import AnswerPlan, _DEMOGRAPHIC, _has
from applypilot.apply.greenhouse_submit import FormAction, RequiredFormFieldsError


_DISCOVERY_JS = r"""() => [...document.querySelectorAll(
  '.ashby-application-form-field-entry[data-field-path]'
)].map(entry => {
  const path = entry.getAttribute('data-field-path') || '';
  const label = (entry.querySelector('label')?.innerText || '').trim();
  const control = entry.querySelector('input, textarea, select');
  let type = control?.type || control?.tagName?.toLowerCase() || '';
  if (path === '_systemfield_location') type = 'location';
  else if (control?.type === 'checkbox' && entry.querySelectorAll('button').length >= 2) {
    type = 'boolean';
  } else if (control?.tagName === 'TEXTAREA') type = 'textarea';
  else if (control?.tagName === 'SELECT') type = 'select';
  return {
    path, label, type,
    required: !!control?.required || !!entry.querySelector('label[class*="required"]'),
    options: control?.tagName === 'SELECT'
      ? [...control.options].filter(option => option.value).map(option => ({
          label: (option.text || '').trim(), value: option.value,
        })) : [],
  };
}).filter(field => field.path && field.label)"""


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def adapter_enabled() -> bool:
    return _flag("APPLYPILOT_ASHBY_ADAPTER")


def submit_enabled() -> bool:
    return adapter_enabled() and _flag("APPLYPILOT_ASHBY_ADAPTER_SUBMIT")


def parse_ashby_url(url: str) -> tuple[str, str] | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except (TypeError, ValueError):
        return None
    if (parsed.hostname or "").lower() != "jobs.ashbyhq.com":
        return None
    parts = [part for part in parsed.path.split("/") if part and part != "application"]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def discover_fields(page) -> list[dict]:
    return list(page.evaluate(_DISCOVERY_JS) or [])


def _location(personal: dict) -> str | None:
    city = str(personal.get("city") or "").strip()
    state = str(personal.get("province_state") or "").strip()
    country = str(personal.get("country") or "").strip()
    if country.lower() in {"us", "usa", "united states of america"}:
        country = "United States"
    return ", ".join(value for value in (city, state, country) if value) or None


def build_ashby_plan(fields, *, profile, resume_text, corpus=None, answer_fn=None, job=None):
    if answer_fn is None:
        from applypilot.apply.answerer import answer_question
        answer_fn = answer_question

    personal = (profile or {}).get("personal") or {}
    work_auth = (profile or {}).get("work_authorization") or {}
    compensation = (profile or {}).get("compensation") or {}
    mapped: dict = {}
    free_text: dict = {}
    resume_field = None
    unmapped: list[str] = []

    for field in fields or []:
        path = str(field.get("path") or "")
        label = str(field.get("label") or path).strip()
        low = label.lower()
        field_type = str(field.get("type") or "")
        required = bool(field.get("required"))
        value = None

        if path == "_systemfield_name":
            value = personal.get("full_name")
        elif path == "_systemfield_email":
            value = personal.get("email")
        elif path == "_systemfield_resume":
            resume_field = path
            value = "__resume__"
        elif path == "_systemfield_location" or field_type == "location":
            value = _location(personal)
        elif "linkedin" in low:
            value = personal.get("linkedin_url") or personal.get("linkedin")
        elif any(token in low for token in ("website", "portfolio", "personal site")):
            value = personal.get("portfolio_url") or personal.get("website_url")
        elif "compensation" in low or "salary" in low:
            minimum = compensation.get("salary_range_min")
            maximum = compensation.get("salary_range_max")
            value = (
                f"${minimum}-${maximum}"
                if minimum and maximum
                else compensation.get("salary_expectation")
            )
        elif field_type == "boolean":
            if "sponsor" in low or "visa" in low:
                sponsor = work_auth.get("require_sponsorship")
                if sponsor is not None:
                    value = not (str(sponsor).strip().lower() in {"no", "false", "0", "n"})
            elif "authorized" in low or "eligible to work" in low:
                authorized = work_auth.get("legally_authorized_to_work")
                if authorized is not None:
                    value = str(authorized).strip().lower() in {"yes", "true", "1", "y"}
            elif _has(low, _DEMOGRAPHIC):
                value = None
        elif field_type == "textarea":
            result = answer_fn(
                label,
                job=job or {},
                profile=profile,
                resume_text=resume_text,
                corpus=corpus,
                kind="open",
            )
            if getattr(result, "verified", False):
                value = result.text
                free_text[path] = result.text

        if value not in (None, ""):
            mapped[path] = value
        elif required:
            unmapped.append(label)

    ready = bool(mapped.get("_systemfield_name") and mapped.get("_systemfield_email")) and not unmapped
    return AnswerPlan(
        fields=mapped,
        resume_field=resume_field,
        free_text=free_text,
        unmapped_required=unmapped,
        ready=ready,
    )


def plan_ashby_actions(plan: AnswerPlan, fields, *, resume_path=None, include_submit=True):
    types = {field.get("path"): field.get("type") for field in fields or []}
    actions: list[FormAction] = []
    if plan.resume_field and resume_path:
        actions.append(FormAction("file", f'[id="{plan.resume_field}"]', resume_path))
    for path, value in plan.fields.items():
        if path == plan.resume_field:
            continue
        field_type = types.get(path)
        entry = f'[data-field-path="{path}"]'
        if field_type == "location":
            actions.append(FormAction("ashby_location", f'{entry} input[role="combobox"]', value))
        elif field_type == "boolean":
            actions.append(FormAction("ashby_boolean", entry, bool(value)))
        elif field_type == "textarea":
            actions.append(FormAction("textarea", f'[id="{path}"]', value))
        elif field_type == "select":
            actions.append(FormAction("select", f'[id="{path}"]', value))
        else:
            actions.append(FormAction("fill", f'[id="{path}"]', value))
    if include_submit:
        actions.append(FormAction("submit", 'button:has-text("Submit Application")'))
    return actions


def execute_ashby_actions(actions, page, *, dry_run=True, before_submit=None):
    filled = []
    submitted = False
    for action in actions:
        if action.kind == "submit":
            if dry_run:
                continue
            invalid = page.locator("form").locator(":invalid").evaluate_all(
                "elements => elements.map(element => element.id || element.name || element.type)"
            )
            if invalid:
                raise RequiredFormFieldsError([str(item) for item in invalid])
            if before_submit is not None:
                before_submit()
            page.get_by_role("button", name="Submit Application", exact=True).click()
            submitted = True
        elif action.kind == "file":
            page.set_input_files(action.selector, action.value)
            filled.append(action.selector)
        elif action.kind == "ashby_location":
            control = page.locator(action.selector)
            control.click()
            control.fill("")
            control.press_sequentially(str(action.value), delay=50)
            page.get_by_role("option", name=str(action.value), exact=True).click()
            filled.append(action.selector)
        elif action.kind == "ashby_boolean":
            page.locator(action.selector).get_by_role(
                "button", name="Yes" if action.value else "No", exact=True,
            ).click()
            filled.append(action.selector)
        elif action.kind == "textarea":
            page.fill(action.selector, action.value)
            filled.append(action.selector)
        elif action.kind == "select":
            page.select_option(action.selector, str(action.value))
            filled.append(action.selector)
        else:
            page.fill(action.selector, action.value)
            filled.append(action.selector)
    return {"filled": filled, "submitted": submitted, "dry_run": dry_run}


def verify_ashby_submission(page) -> bool:
    try:
        page.wait_for_function(
            """() => {
                const text = (document.body?.innerText || '').toLowerCase();
                const path = window.location.pathname.toLowerCase();
                return path.includes('confirmation') || path.includes('submitted') ||
                    text.includes('application submitted') ||
                    text.includes('thank you for applying') ||
                    text.includes('thanks for applying');
            }""",
            timeout=15_000,
        )
        return True
    except Exception:
        return False
