from __future__ import annotations

import pytest

from applypilot.apply.workday_adapter import WorkdayApplicationRun, evaluate_confirmation


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "final_url": "https://acme.wd5.myworkdayjobs.com/application/confirmation",
            "page_text": "Review",
        },
        {
            "final_url": "https://acme.wd5.myworkdayjobs.com/review",
            "page_text": "Application submitted. Thank you for applying.",
        },
    ],
)
def test_positive_url_or_dom_evidence_confirms_application(kwargs):
    decision = evaluate_confirmation(**kwargs, submit_clicked=True)
    assert decision.status == "applied"
    assert decision.confirmed is True
    assert decision.evidence[0].authoritative is True


def test_attributed_inbox_event_can_confirm_application():
    job_url = "https://acme.wd5.myworkdayjobs.com/job/1"
    decision = evaluate_confirmation(
        final_url=None,
        page_text=None,
        submit_clicked=True,
        job_url=job_url,
        inbox_events=[{
            "message_id": "workday-ack-1",
            "job_url": job_url,
            "stage": "acknowledged",
            "match_status": "attributed",
        }],
    )
    assert decision.status == "applied"
    assert decision.evidence[0].kind == "inbox_event"


def test_wrong_job_or_needs_review_email_is_not_confirmation():
    decision = evaluate_confirmation(
        final_url=None,
        page_text=None,
        submit_clicked=True,
        job_url="job-1",
        inbox_events=[{
            "message_id": "ambiguous",
            "job_url": "job-2",
            "stage": "acknowledged",
            "match_status": "needs_review",
        }],
    )
    assert decision.status == "no_confirmation"
    assert decision.confirmed is False


def test_submit_click_without_confirmation_is_terminal_no_confirmation():
    decision = evaluate_confirmation(
        final_url="https://acme.wd5.myworkdayjobs.com/review",
        page_text="Your application is ready to submit",
        submit_clicked=True,
    )
    assert decision.status == "no_confirmation"
    assert decision.reason == "submit_clicked_without_confirmation"


def test_without_submit_or_confirmation_is_not_submitted():
    decision = evaluate_confirmation(
        final_url=None,
        page_text="Review your application",
        submit_clicked=False,
    )
    assert decision.status == "not_submitted"


def test_synthetic_full_run_reaches_confirmation_without_model_navigation():
    run = WorkdayApplicationRun()
    snapshots = [
        {"automation_ids": ["signInContent"]},
        {"automation_ids": ["file-upload-input-ref"]},
        {"automation_ids": ["contactInformationPage"]},
        {"automation_ids": ["workExperienceSection"]},
        {"automation_ids": ["applicationQuestionsPage"]},
        {"automation_ids": ["voluntaryDisclosuresPage"]},
        {"automation_ids": ["selfIdentificationPage"]},
        {"automation_ids": ["reviewPage"]},
    ]
    assert all(run.observe(snapshot).allowed for snapshot in snapshots)
    assert run.mark_submit_clicked().allowed
    decision = run.finish(
        final_url="https://acme.wd5.myworkdayjobs.com/application/confirmation",
        page_text="Application submitted",
    )
    assert decision.status == "applied"
    metadata = run.metadata(decision)
    assert metadata["current_state"] == "confirmation"
    assert metadata["submit_clicked"] is True
    assert metadata["invalid_transitions"] == 0
    assert metadata["confirmation_evidence"]
