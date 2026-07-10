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
from dataclasses import dataclass, field

from applypilot.apply.greenhouse_adapter import AnswerPlan


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
        if name.endswith("[]"):
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
        if ftype == "textarea":
            actions.append(FormAction("textarea", field_selector, value))
        elif ftype in ("multi_value_single_select", "multi_value_multi_select"):
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
                 before_submit=None) -> SubmitReport:
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
            if before_submit is not None:
                before_submit()
            page.click(a.selector)
            report.submitted = True
            continue
        if a.kind == "file":
            page.set_input_files(a.selector, a.value)
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
                page.get_by_role("option", name=a.option_label, exact=True).click()
            else:
                page.select_option(a.selector, a.value)
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
                     before_submit=None, verify_fn=None) -> dict:
    """End-to-end: parse -> fetch questions -> plan -> route.

    When the plan is complete (``ready``) it fills the form deterministically
    against ``page`` (dry-run by default -- no submit). When it isn't, it returns
    an ``agent_fallback`` decision WITHOUT touching the form, so the caller hands
    off to the existing apply agent (passing the plan makes even that cheaper).
    """
    from applypilot.apply.greenhouse_adapter import (
        build_answer_plan,
        fetch_questions,
        parse_greenhouse_url,
    )

    parsed = parse_greenhouse_url(job_url)
    if not parsed:
        return {"route": "not_greenhouse"}
    board, job_id = parsed

    questions = fetch_questions(board, job_id, fetch=fetch)
    plan = build_answer_plan(questions, profile=profile, resume_text=resume_text,
                             corpus=corpus, answer_fn=answer_fn, job={"site": board})
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
    )
    result = {"route": "deterministic", "plan": plan, "actions": actions,
              "report": report, "ready": True,
              "attempt_context": attempt_context}
    if report.submitted:
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
