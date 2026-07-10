"""Deterministic Greenhouse submit path (+ agent-fallback routing).

Turns an :class:`AnswerPlan` into an ordered list of deterministic browser
actions and executes them against a page. This is the "option 2" deterministic
DOM filler; ``decide_route`` is the "option 1" glue -- when the plan isn't
complete (a required field couldn't be mapped), the caller hands off to the
existing apply agent instead of the deterministic path.

SAFETY: ``execute_form`` defaults to ``dry_run=True`` -- it fills every field
but NEVER clicks the submit button. A real submission requires an explicit
``dry_run=False`` from a caller that has already cleared the apply lane's
canary / approval / cost guards.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

from applypilot.apply.greenhouse_adapter import AnswerPlan


class RequiredFormFieldsError(RuntimeError):
    def __init__(self, fields: list[str]):
        self.fields = fields
        super().__init__("required Greenhouse fields remain invalid: " + ", ".join(fields))


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def adapter_enabled() -> bool:
    """Opt-in gate for the live-apply hook. OFF by default so production apply
    behaviour is unchanged until the owner sets APPLYPILOT_GREENHOUSE_ADAPTER."""
    return _flag("APPLYPILOT_GREENHOUSE_ADAPTER")


def submit_enabled() -> bool:
    """Second, independent gate: lets the adapter OWN a real submission (fill +
    click submit + record the outcome). OFF by default so turning on shadow
    validation can never accidentally start submitting. Requires
    APPLYPILOT_GREENHOUSE_ADAPTER_SUBMIT in addition to the adapter flag."""
    return _flag("APPLYPILOT_GREENHOUSE_ADAPTER_SUBMIT")


# Positive proof that the application went through. Absent any of these we report
# no_confirmation -- a false "applied" is worse than a failure.
_SUCCESS_MARKERS = (
    "thank you for applying",
    "thanks for applying",
    "application received",
    "application submitted",
    "your application has been submitted",
    "we've received your application",
    "we have received your application",
    "successfully submitted",
)

_POST_SUBMIT_SETTLED = """() => {
    const text = (document.body?.innerText || '').toLowerCase();
    const path = window.location.pathname.toLowerCase();
    return path.includes('/confirmation') || path.includes('/thank') ||
        path.includes('/submitted') ||
        text.includes('thank you for applying') ||
        text.includes('thanks for applying') ||
        text.includes('application received') ||
        text.includes('application submitted') ||
        text.includes('your application has been submitted') ||
        text.includes("we've received your application") ||
        text.includes('we have received your application') ||
        text.includes('successfully submitted') ||
        text.includes('please correct') || text.includes('errors below') ||
        text.includes('field is required');
}"""


def _wait_for_submission_settlement(page, *, timeout_ms: int = 15_000) -> str | None:
    """Wait for Greenhouse's async POST to produce success or validation UI."""
    wait_for_function = getattr(page, "wait_for_function", None)
    if wait_for_function is None:
        return None
    try:
        wait_for_function(_POST_SUBMIT_SETTLED, timeout=timeout_ms)
        return None
    except Exception as exc:
        # The independent verifier below remains fail-closed on timeout/browser errors.
        return type(exc).__name__


def _safe_url(url: object) -> str | None:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))[:500]


def _is_expected_submit_response(response, expected_url: str) -> bool:
    try:
        method = str(response.request.method or "").upper()
        return method == "POST" and _safe_url(response.url) == _safe_url(expected_url)
    except Exception:
        return False


def _response_request_id(response) -> str | None:
    header_value = getattr(response, "header_value", None)
    if header_value is None:
        return None
    for name in ("x-request-id", "x-amzn-trace-id", "traceparent"):
        try:
            value = str(header_value(name) or "").strip()
        except Exception:
            continue
        if value:
            return value[:300]
    return None


def _response_code(response) -> str | None:
    try:
        payload = response.json()
    except Exception:
        return None
    code = str(payload.get("code") or "") if isinstance(payload, dict) else ""
    return code if re.fullmatch(r"[a-z0-9_-]{1,80}", code) else None


def _click_for_response(page, selector: str, expected_url: str | None) -> dict:
    observation = {
        "clicked": False,
        "status": None,
        "url": None,
        "request_id": None,
        "code": None,
        "wait_error": None,
    }
    expect_response = getattr(page, "expect_response", None)
    if expected_url and expect_response is not None:
        try:
            with expect_response(
                lambda response: _is_expected_submit_response(response, expected_url),
                timeout=15_000,
            ) as response_info:
                page.click(selector)
                observation["clicked"] = True
            response = response_info.value
            observation.update({
                "status": int(response.status),
                "url": _safe_url(response.url),
                "request_id": _response_request_id(response),
                "code": _response_code(response),
            })
        except Exception as exc:
            observation["wait_error"] = type(exc).__name__
    if not observation["clicked"]:
        page.click(selector)
        observation["clicked"] = True
    return observation


def _received_at(raw: str | None) -> datetime | None:
    try:
        value = parsedate_to_datetime(raw or "")
    except Exception:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalized_company(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _wait_for_greenhouse_security_code(
    *, board: str, company_name: str, not_before: datetime,
    timeout_seconds: int = 90,
) -> str | None:
    from applypilot import inbox_auth
    from applypilot.mail_source import get_mail_source

    expected = {
        item for item in (_normalized_company(board), _normalized_company(company_name)) if item
    }
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            messages = get_mail_source().fetch(
                since_days=1,
                max_messages=50,
                gmail_raw_query='"security code" OR "verification code"',
            )
            matches = inbox_auth.scan_gmail_for_auth_codes(
                messages=messages, minutes=10, max_messages=50,
            )
            matches.sort(
                key=lambda match: _received_at(match.received_at)
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            for match in matches:
                received = _received_at(match.received_at)
                subject = _normalized_company(match.subject)
                candidate = match.candidate
                if (
                    received is not None
                    and received >= not_before - timedelta(seconds=5)
                    and candidate.kind == "code"
                    and re.fullmatch(r"\d{8}", candidate.value)
                    and any(item in subject for item in expected)
                ):
                    return candidate.value
        except Exception:
            pass
        time.sleep(3)
    return None


def _complete_greenhouse_email_challenge(
    page, *, board: str, company_name: str, expected_submit_url: str,
    report, code_fn=None,
) -> bool:
    if report.response_status != 428 or report.response_code != "captcha-failed":
        return False
    try:
        page.wait_for_selector("#security-input-0", state="visible", timeout=10_000)
    except Exception:
        return False
    not_before = datetime.now(timezone.utc)
    fetch_code = code_fn or _wait_for_greenhouse_security_code
    code = fetch_code(
        board=board,
        company_name=company_name,
        not_before=not_before,
    )
    if not code or not re.fullmatch(r"\d{8}", str(code)):
        return False
    for index, digit in enumerate(str(code)):
        page.fill(f"#security-input-{index}", digit)
    report.challenge_response_status = report.response_status
    report.challenge_response_code = report.response_code
    observation = _click_for_response(page, _SUBMIT_SELECTOR, expected_submit_url)
    report.response_status = observation["status"]
    report.response_url = observation["url"]
    report.response_request_id = observation["request_id"]
    report.response_code = observation["code"]
    report.response_wait_error = observation["wait_error"]
    report.security_code_used = True
    return True


def _collect_submission_diagnostics(page, report) -> dict:
    diagnostics = {
        "response_status": report.response_status,
        "response_url": report.response_url,
        "response_request_id": report.response_request_id,
        "response_code": report.response_code,
        "response_wait_error": report.response_wait_error,
        "challenge_response_status": report.challenge_response_status,
        "challenge_response_code": report.challenge_response_code,
        "security_code_used": report.security_code_used,
        "settlement_error": report.settlement_error,
        "final_url": _safe_url(getattr(page, "url", None)),
        "invalid_fields": [],
        "validation_messages": [],
    }
    evaluate = getattr(page, "evaluate", None)
    if evaluate is not None:
        try:
            dom = evaluate("""() => {
                const visible = element => !!(element.offsetWidth || element.offsetHeight ||
                    element.getClientRects().length);
                const invalidFields = [...document.querySelectorAll('[aria-invalid="true"]')]
                    .filter(visible)
                    .map(element => element.id || element.getAttribute('name') || element.tagName)
                    .filter(Boolean).slice(0, 20);
                const messages = [...document.querySelectorAll(
                    '[role="alert"], .field-error, .error-message, [id$="-error"]')]
                    .filter(visible)
                    .map(element => (element.innerText || element.textContent || '').trim())
                    .filter(Boolean).slice(0, 20);
                return {invalid_fields: invalidFields, validation_messages: messages};
            }""")
            if isinstance(dom, dict):
                diagnostics["invalid_fields"] = [
                    str(item)[:200] for item in (dom.get("invalid_fields") or [])[:20]
                ]
                diagnostics["validation_messages"] = [
                    str(item)[:300] for item in (dom.get("validation_messages") or [])[:20]
                ]
        except Exception:
            pass
    return diagnostics


def _invalid_required_controls(page) -> list[str]:
    locator = getattr(page, "locator", None)
    if locator is None:
        return []
    try:
        form = locator("form")
        if form.count() == 0 or form.evaluate("form => form.checkValidity()"):
            return []
        return form.locator(":invalid").evaluate_all("""elements => elements.map(element => {
            if (element.id || element.name) return element.id || element.name;
            const container = element.parentElement?.parentElement;
            const label = container?.querySelector('label, legend');
            return (label?.innerText || container?.innerText || element.tagName)
                .replace(/\\s+/g, ' ').trim().slice(0, 200);
        }).filter(Boolean).slice(0, 30)""")
    except Exception:
        return []


def _page_content(page) -> str:
    try:
        return page.content() or ""
    except Exception:
        return ""


def verify_greenhouse_submission(page):
    """Normalize Greenhouse page evidence through the independent verifier."""
    from applypilot.apply.submission_verifier import (
        SubmissionEvidence,
        verify_submission,
    )

    content = _page_content(page)
    lowered = content.lower()
    validation_errors = ()
    if any(
        marker in lowered
        for marker in ("please correct", "errors below", "field is required")
    ):
        validation_errors = ("greenhouse form validation error",)
    page_url = str(getattr(page, "url", "") or "")
    return verify_submission(
        SubmissionEvidence(
            page_url=page_url,
            allowed_success_hosts=("greenhouse.io",),
            success_url_markers=("/confirmation", "/thank", "/submitted"),
            dom_text=content,
            validation_errors=validation_errors,
        )
    )


def detect_confirmation(page) -> str:
    """Inspect the post-submit page. Return 'applied' only on positive
    confirmation, else 'failed:no_confirmation'. Never raises."""
    result = verify_greenhouse_submission(page)
    return "applied" if result.status == "verified" else "failed:no_confirmation"

# Greenhouse hosted-form input ids equal the API field name (e.g. id="first_name",
# id="question_12074265004"); the submit button is id="submit_app".
_SUBMIT_SELECTOR = "button[type='submit']"


@dataclass
class FormAction:
    kind: str          # "fill" | "textarea" | "file" | "select" | "submit"
    selector: str
    value: object = None
    option_label: str | None = None


@dataclass
class SubmitReport:
    filled: list = field(default_factory=list)   # selectors acted on (excluding submit)
    submitted: bool = False
    dry_run: bool = True
    skipped_submit: bool = False
    response_status: int | None = None
    response_url: str | None = None
    response_request_id: str | None = None
    response_code: str | None = None
    response_wait_error: str | None = None
    settlement_error: str | None = None
    challenge_response_status: int | None = None
    challenge_response_code: str | None = None
    security_code_used: bool = False


def plan_form_actions(plan: AnswerPlan, questions, *, resume_path=None) -> list[FormAction]:
    """Turn an AnswerPlan into an ordered list of deterministic form actions.

    ``questions`` supplies each field's type so the executor knows whether to
    fill text, fill a textarea, upload a file, or pick a select option.
    """
    types: dict = {}
    option_labels: dict = {}
    for q in questions or []:
        for f in q.get("fields", []) or []:
            name = f.get("name")
            types[name] = f.get("type")
            option_labels[name] = {
                value.get("value"): value.get("label")
                for value in (f.get("values") or [])
            }

    def selector(name: str) -> str:
        if name.endswith("[]") or (name and name[0].isdigit()):
            return f'[id="{name}"]'
        return f"#{name}"

    actions: list[FormAction] = []
    if plan.resume_field and resume_path:
        actions.append(FormAction("file", selector(plan.resume_field), resume_path))

    for name, value in plan.fields.items():
        if name == "resume_text" and plan.resume_field and resume_path:
            continue
        field_selector = selector(name)
        ftype = types.get(name)
        if ftype == "location_autocomplete":
            actions.append(FormAction("location", "#candidate-location", value))
        elif ftype == "textarea":
            actions.append(FormAction("textarea", field_selector, value))
        elif ftype == "phone_country_select":
            actions.append(FormAction(
                "phone_country",
                field_selector,
                value,
                option_label=option_labels.get(name, {}).get(value),
            ))
        elif ftype in (
            "multi_value_single_select", "multi_value_multi_select", "react_select",
        ):
            actions.append(FormAction(
                "select",
                field_selector,
                value,
                option_label=option_labels.get(name, {}).get(value),
            ))
        else:
            actions.append(FormAction("fill", field_selector, value))

    actions.append(FormAction("submit", _SUBMIT_SELECTOR))
    return actions


def execute_form(actions, page, *, dry_run: bool = True,
                 before_submit=None, expected_submit_url: str | None = None) -> SubmitReport:
    """Run form actions against ``page``. Dry-run (default) never clicks submit.

    ``page`` is any object exposing ``fill``, ``set_input_files``,
    ``select_option`` and ``click`` (a Playwright Page, or a fake in tests).
    """
    report = SubmitReport(dry_run=dry_run)
    for a in actions:
        if a.kind == "submit":
            if dry_run:
                report.skipped_submit = True
                continue
            invalid_fields = _invalid_required_controls(page)
            if invalid_fields:
                raise RequiredFormFieldsError(invalid_fields)
            if before_submit is not None:
                before_submit()
            observation = _click_for_response(page, a.selector, expected_submit_url)
            report.response_status = observation["status"]
            report.response_url = observation["url"]
            report.response_request_id = observation["request_id"]
            report.response_code = observation["code"]
            report.response_wait_error = observation["wait_error"]
            report.submitted = True
            continue
        if a.kind == "file":
            page.set_input_files(a.selector, a.value)
        elif a.kind == "location":
            location_input = page.locator(a.selector)
            location_input.click()
            location_input.fill("")
            location_input.press_sequentially(str(a.value), delay=50)
            page.get_by_role("option", name=str(a.value), exact=True).click()
        elif a.kind == "phone_country":
            input_locator = page.locator(a.selector)
            container = input_locator.locator(
                'xpath=ancestor::div[contains(@class,"select__container")]'
            )
            container.get_by_role("button").click()
            listbox_id = input_locator.get_attribute("aria-controls")
            if not listbox_id or not a.option_label:
                raise RuntimeError("phone country options unavailable")
            page.locator(f"#{listbox_id}").get_by_role(
                "option", name=a.option_label, exact=True,
            ).click()
        elif a.kind == "select":
            tag_name = None
            if hasattr(page, "locator"):
                try:
                    tag_name = page.locator(a.selector).evaluate(
                        "element => element.tagName.toLowerCase()"
                    )
                except Exception:
                    tag_name = None
            if tag_name == "input" and a.option_label and hasattr(page, "get_by_role"):
                page.click(a.selector)
                page.fill(a.selector, a.option_label)
                page.get_by_role("option", name=a.option_label, exact=True).click()
            else:
                page.select_option(a.selector, str(a.value))
        else:  # fill / textarea
            page.fill(a.selector, a.value)
        report.filled.append(a.selector)
    return report


def decide_route(plan: AnswerPlan) -> tuple[str, list]:
    """('deterministic', []) when the plan is complete, else
    ('agent_fallback', [required labels we couldn't map])."""
    if plan.ready:
        return ("deterministic", [])
    return ("agent_fallback", list(plan.unmapped_required))


def capture_answers(plan, questions, job, *, remember_fn=None) -> int:
    """Append each verified free-text answer to the corpus (question label ->
    answer) so retrieval compounds over time. Returns how many were captured."""
    if remember_fn is None:
        from applypilot.apply.answerer import remember_answer as remember_fn

    labels: dict = {}
    for q in questions or []:
        for f in q.get("fields", []) or []:
            labels[f.get("name")] = q.get("label", "")

    captured = 0
    for name, text in (plan.free_text or {}).items():
        label = labels.get(name)
        if label and text:
            remember_fn(label, text, job=job)
            captured += 1
    return captured


def apply_greenhouse(job_url, *, profile, resume_text, resume_path, page,
                     corpus=None, fetch=None, answer_fn=None, remember_fn=None,
                      dry_run: bool = True, on_plan_ready=None,
                      before_submit=None, verify_fn=None, auth_code_fn=None) -> dict:
    """End-to-end: parse -> fetch questions -> plan -> route.

    When the plan is complete (``ready``) it fills the form deterministically
    against ``page`` (dry-run by default -- no submit). When it isn't, it returns
    an ``agent_fallback`` decision WITHOUT touching the form, so the caller hands
    off to the existing apply agent (passing the plan makes even that cheaper).
    """
    from applypilot.apply.greenhouse_adapter import (
        builtin_questions_from_payload,
        build_answer_plan,
        fetch_job,
        job_context_from_payload,
        parse_greenhouse_url,
    )

    parsed = parse_greenhouse_url(job_url)
    if not parsed:
        return {"route": "not_greenhouse"}
    board, job_id = parsed

    payload = fetch_job(board, job_id, fetch=fetch)
    questions = list(payload.get("questions") or [])
    questions.extend(builtin_questions_from_payload(payload, profile=profile))
    plan = build_answer_plan(questions, profile=profile, resume_text=resume_text,
                             corpus=corpus, answer_fn=answer_fn,
                             job=job_context_from_payload(payload, board=board))
    route, reasons = decide_route(plan)
    if route != "deterministic":
        return {"route": "agent_fallback", "plan": plan, "unmapped": reasons, "ready": False}

    actions = plan_form_actions(plan, questions, resume_path=resume_path)
    attempt_context = None
    if not dry_run and on_plan_ready is not None:
        attempt_context = on_plan_ready(plan, actions)

    checkpoint = None
    if not dry_run and before_submit is not None:
        def checkpoint():
            return before_submit(attempt_context)
    report = execute_form(
        actions,
        page,
        dry_run=dry_run,
        before_submit=checkpoint,
        expected_submit_url=f"https://boards.greenhouse.io/{board}/jobs/{job_id}",
    )
    result = {"route": "deterministic", "plan": plan, "actions": actions,
              "report": report, "ready": True,
              "attempt_context": attempt_context}
    if report.submitted:
        _complete_greenhouse_email_challenge(
            page,
            board=board,
            company_name=str(payload.get("company_name") or board),
            expected_submit_url=f"https://boards.greenhouse.io/{board}/jobs/{job_id}",
            report=report,
            code_fn=auth_code_fn,
        )
        report.settlement_error = _wait_for_submission_settlement(page)
        result["submission_diagnostics"] = _collect_submission_diagnostics(page, report)
        verification = (verify_fn or verify_greenhouse_submission)(page)
        result["verification_status"] = verification.status
        result["verification_method"] = verification.method
        result["verification_ref"] = verification.reference
        if verification.status == "verified":
            result["status"] = "applied"
            result["captured"] = capture_answers(plan, questions, {"site": board},
                                                  remember_fn=remember_fn)
        elif verification.status == "contradicted":
            result["status"] = "failed:validation_error"
        else:
            result["status"] = "crash_unconfirmed"
    return result
