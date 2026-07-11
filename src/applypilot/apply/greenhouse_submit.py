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
from urllib.parse import parse_qs, urlparse
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


_SUCCESS_PATH_PARTS = frozenset({"confirmation", "confirmed", "thank-you", "thank_you", "thanks"})


def _completion_url_confirmed(url: str) -> bool:
    """Accept only explicit completion routes or query values, never a generic job URL."""
    try:
        parsed = urlparse(url or "")
        parts = {part.lower() for part in parsed.path.split("/") if part}
        if parts & _SUCCESS_PATH_PARTS:
            return True
        query = parse_qs(parsed.query)
        values = {value.lower() for group in query.values() for value in group}
        return bool(values & {"submitted", "complete", "completed", "success"})
    except Exception:
        return False


def detect_confirmation(page, *, inbox_confirmed: bool = False) -> str:
    """Inspect the post-submit page. Return 'applied' only on positive
    confirmation, else 'failed:no_confirmation'. Never raises."""
    if inbox_confirmed:
        return "applied"
    try:
        html = (page.content() or "").lower()
    except Exception:
        html = ""
    if any(marker in html for marker in _SUCCESS_MARKERS):
        return "applied"
    try:
        if _completion_url_confirmed(str(page.url)):
            return "applied"
    except Exception:
        pass
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
    submit_attempted: bool = False
    submitted: bool = False
    dry_run: bool = True
    skipped_submit: bool = False
    submit_error: str | None = None


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
            # Record ownership before dispatch. A browser exception can happen after
            # the click reached the page, so retrying through an agent would risk a
            # duplicate application.
            report.submit_attempted = True
            try:
                page.click(a.selector)
                report.submitted = True
            except Exception as exc:
                report.submit_error = type(exc).__name__
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
                     inbox_confirmation_fn=None,
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
    if report.submit_attempted:
        inbox_confirmed = False
        if inbox_confirmation_fn is not None:
            try:
                inbox_confirmed = bool(inbox_confirmation_fn(board=board, job_id=job_id))
            except Exception:
                inbox_confirmed = False
        result["status"] = detect_confirmation(page, inbox_confirmed=inbox_confirmed)
        if result["status"] == "applied":
            try:
                result["captured"] = capture_answers(
                    plan, questions, {"site": board}, remember_fn=remember_fn
                )
            except Exception:
                # Corpus persistence is an optimization after submission. It cannot
                # invalidate positive submission evidence or release agent fallback.
                result["captured"] = 0
                result["capture_failed"] = True
    return result
