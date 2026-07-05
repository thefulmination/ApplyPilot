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


def detect_confirmation(page) -> str:
    """Inspect the post-submit page. Return 'applied' only on positive
    confirmation, else 'failed:no_confirmation'. Never raises."""
    try:
        html = (page.content() or "").lower()
    except Exception:
        return "failed:no_confirmation"
    if any(marker in html for marker in _SUCCESS_MARKERS):
        return "applied"
    return "failed:no_confirmation"

# Greenhouse hosted-form input ids equal the API field name (e.g. id="first_name",
# id="question_12074265004"); the submit button is id="submit_app".
_SUBMIT_SELECTOR = "#submit_app"


@dataclass
class FormAction:
    kind: str          # "fill" | "textarea" | "file" | "select" | "submit"
    selector: str
    value: object = None


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
    for q in questions or []:
        for f in q.get("fields", []) or []:
            types[f.get("name")] = f.get("type")

    actions: list[FormAction] = []
    if plan.resume_field and resume_path:
        actions.append(FormAction("file", f"#{plan.resume_field}", resume_path))

    for name, value in plan.fields.items():
        selector = f"#{name}"
        ftype = types.get(name)
        if ftype == "textarea":
            actions.append(FormAction("textarea", selector, value))
        elif ftype == "multi_value_single_select":
            actions.append(FormAction("select", selector, value))
        else:
            actions.append(FormAction("fill", selector, value))

    actions.append(FormAction("submit", _SUBMIT_SELECTOR))
    return actions


def execute_form(actions, page, *, dry_run: bool = True) -> SubmitReport:
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
            page.click(a.selector)
            report.submitted = True
            continue
        if a.kind == "file":
            page.set_input_files(a.selector, a.value)
        elif a.kind == "select":
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
                     dry_run: bool = True) -> dict:
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
    report = execute_form(actions, page, dry_run=dry_run)
    result = {"route": "deterministic", "plan": plan, "actions": actions,
              "report": report, "ready": True}
    if report.submitted:
        result["status"] = detect_confirmation(page)
        if result["status"] == "applied":
            result["captured"] = capture_answers(plan, questions, {"site": board},
                                                  remember_fn=remember_fn)
    return result
