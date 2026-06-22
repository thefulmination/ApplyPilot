"""Tests for Gmail outcome auto-detection.

All pure-function tests — no network, no database, no Google API required.
"""

from __future__ import annotations

import pytest

from applypilot.gmail_outcomes import (
    EmailOutcome,
    _extract_domain,
    _extract_company_from_subject,
    _extract_title_from_subject,
    _token_overlap,
    _url_domain,
    apply_outcomes,
    classify_email_outcome,
    match_email_to_job,
)


# ---------------------------------------------------------------------------
# classify_email_outcome
# ---------------------------------------------------------------------------

class TestClassifyEmailOutcome:
    def test_clear_rejection_subject_and_body(self):
        outcome, confidence, signals = classify_email_outcome(
            subject="Unfortunately we won't be moving forward",
            body="After careful consideration we have decided to pursue other candidates.",
        )
        assert outcome == "rejected"
        assert confidence in ("high", "medium")
        assert signals

    def test_rejection_body_only(self):
        outcome, _, _ = classify_email_outcome(
            subject="Your application to Acme",
            body="We regret to inform you that we will not be moving forward with your application.",
        )
        assert outcome == "rejected"

    def test_interview_invitation(self):
        outcome, confidence, _ = classify_email_outcome(
            subject="Interview invitation — Chief of Staff at Acme",
            body="We'd like to schedule a phone screen. Please use the Calendly link.",
        )
        assert outcome == "interview"
        assert confidence in ("high", "medium")

    def test_interview_next_steps(self):
        outcome, _, _ = classify_email_outcome(
            subject="Next steps for your application",
            body="We would like to move forward with your application. "
                 "Please schedule a 30-minute video interview.",
        )
        assert outcome == "interview"

    def test_offer_letter(self):
        outcome, confidence, _ = classify_email_outcome(
            subject="Offer of Employment — Acme Corp",
            body="We are pleased to offer you the position. "
                 "Please review the attached offer letter.",
        )
        assert outcome == "offer"
        assert confidence in ("high", "medium")

    def test_offer_beats_interview(self):
        # Email mentions scheduling a call AND an offer — offer wins
        outcome, _, _ = classify_email_outcome(
            subject="Congratulations — Job Offer",
            body="We'd like to schedule a call to walk through the offer of employment and next steps.",
        )
        assert outcome == "offer"

    def test_ambiguous_acknowledgement(self):
        # "Application received" should not be classified
        outcome, _, _ = classify_email_outcome(
            subject="We received your application",
            body="Thank you for applying. We will be in touch.",
        )
        assert outcome == "ambiguous"

    def test_ats_sender_rejection(self):
        outcome, _, _ = classify_email_outcome(
            subject="Update on your application",
            body="Unfortunately, we have decided not to move forward with your candidacy at this time.",
            sender="no-reply@greenhouse.io",
        )
        assert outcome == "rejected"

    def test_assessment_invite_counts_as_interview(self):
        outcome, _, _ = classify_email_outcome(
            subject="Technical assessment — Software Engineer",
            body="Please complete the HackerRank challenge linked below within 5 days.",
        )
        assert outcome == "interview"

    def test_offer_with_salary_details(self):
        outcome, confidence, _ = classify_email_outcome(
            subject="Your job offer from Globex",
            body="We would like to offer you a salary of $120,000. Your start date of June 30th...",
        )
        assert outcome == "offer"
        assert confidence in ("high", "medium")


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_extract_domain_angle_brackets(self):
        assert _extract_domain("Jane Doe <jane@company.com>") == "company.com"

    def test_extract_domain_bare(self):
        assert _extract_domain("noreply@greenhouse.io") == "greenhouse.io"

    def test_extract_domain_none(self):
        assert _extract_domain("no-email-here") is None

    def test_url_domain_strips_www(self):
        assert _url_domain("https://www.acme.com/jobs/123") == "acme.com"

    def test_url_domain_preserves_subdomain(self):
        assert _url_domain("https://jobs.lever.co/company/role") == "jobs.lever.co"

    def test_url_domain_none(self):
        assert _url_domain("not-a-url") is None

    def test_token_overlap_exact(self):
        assert _token_overlap("acme corp", "Acme Corp") == 1.0

    def test_token_overlap_partial(self):
        score = _token_overlap("acme corporation", "Acme Corp Inc")
        assert 0 < score < 1.0

    def test_token_overlap_empty(self):
        assert _token_overlap("", "Acme") == 0.0

    def test_extract_company_from_subject_at(self):
        result = _extract_company_from_subject("Your application at Acme Corp for Chief of Staff")
        assert result == "Acme Corp"

    def test_extract_company_from_subject_interview(self):
        result = _extract_company_from_subject("Interview with Globex for Strategy Manager")
        assert result == "Globex"

    def test_extract_title_from_subject(self):
        result = _extract_title_from_subject("Application for Chief of Staff at Acme")
        assert result == "Chief of Staff"


# ---------------------------------------------------------------------------
# match_email_to_job
# ---------------------------------------------------------------------------

SAMPLE_JOBS = [
    {
        "url": "https://acme.com/jobs/1",
        "application_url": "https://acme.com/jobs/1",
        "title": "Chief of Staff",
        "site": "Acme Corp",
        "company": "Acme Corp",
        "apply_status": "applied",
    },
    {
        "url": "https://greenhouse.io/globex/456",
        "application_url": None,
        "title": "Strategy Manager",
        "site": "Globex",
        "company": "Globex",
        "apply_status": "applied",
    },
    {
        "url": "https://initech.com/jobs/789",
        "application_url": "https://initech.com/jobs/789",
        "title": "Business Development Lead",
        "site": "Initech",
        "company": "Initech",
        "apply_status": "applied",
    },
]


class TestMatchEmailToJob:
    def test_company_domain_match(self):
        job, method, score = match_email_to_job(
            sender="recruiting@initech.com",
            subject="Your application at Initech",
            body="",
            applied_jobs=SAMPLE_JOBS,
        )
        assert job is not None
        assert job["title"] == "Business Development Lead"
        assert method == "company_domain"
        assert score == 1.0

    def test_subdomain_company_domain(self):
        # hr.acme.com should match acme.com job URL
        job, method, _ = match_email_to_job(
            sender="hr@hr.acme.com",
            subject="Interview",
            body="",
            applied_jobs=SAMPLE_JOBS,
        )
        assert job is not None
        assert job["site"] == "Acme Corp"
        assert method == "company_domain"

    def test_generic_domain_falls_through_to_name(self):
        # gmail.com sender can't use domain match; should try company name
        job, method, _ = match_email_to_job(
            sender="recruiter@gmail.com",
            subject="Your application at Acme Corp — next steps",
            body="",
            applied_jobs=SAMPLE_JOBS,
        )
        assert job is not None
        assert "Acme" in (job.get("company") or job.get("site") or "")

    def test_ats_sender_uses_company_from_subject(self):
        job, method, _ = match_email_to_job(
            sender="noreply@greenhouse.io",
            subject="Interview with Globex for Strategy Manager",
            body="",
            applied_jobs=SAMPLE_JOBS,
        )
        assert job is not None
        assert job["site"] == "Globex"
        assert method == "ats_domain"

    def test_no_match_when_unrelated(self):
        job, method, score = match_email_to_job(
            sender="newsletter@someotherdomain.com",
            subject="Weekly digest",
            body="Your weekly newsletter from...",
            applied_jobs=SAMPLE_JOBS,
        )
        assert job is None
        assert method is None

    def test_empty_job_list(self):
        job, method, score = match_email_to_job(
            sender="hr@acme.com",
            subject="Interview",
            body="",
            applied_jobs=[],
        )
        assert job is None


# ---------------------------------------------------------------------------
# apply_outcomes (no DB — dry_run only + ambiguous/no-match skips)
# ---------------------------------------------------------------------------

class TestApplyOutcomes:
    def test_dry_run_counts_but_does_not_write(self):
        outcomes = [
            EmailOutcome(
                message_id="abc", date="Mon, 01 Jan 2024", sender="hr@acme.com",
                subject="Offer!", outcome="offer", confidence="high",
                matched_job_url="https://acme.com/jobs/1",
                matched_job_title="Chief of Staff",
            ),
        ]
        result = apply_outcomes(outcomes, dry_run=True)
        assert result["written"] == 1
        assert result["errors"] == 0

    def test_skips_no_match(self):
        outcomes = [
            EmailOutcome(
                message_id="xyz", date="Mon, 01 Jan 2024", sender="hr@unknown.com",
                subject="Interview", outcome="interview", confidence="high",
                matched_job_url=None,
            ),
        ]
        result = apply_outcomes(outcomes, dry_run=True)
        assert result["written"] == 0
        assert result["skipped_no_match"] == 1

    def test_skips_ambiguous(self):
        outcomes = [
            EmailOutcome(
                message_id="xyz", date="Mon, 01 Jan 2024", sender="hr@acme.com",
                subject="Your application", outcome="ambiguous", confidence="low",
                matched_job_url="https://acme.com/jobs/1",
            ),
        ]
        result = apply_outcomes(outcomes, dry_run=True)
        assert result["written"] == 0
        assert result["skipped_ambiguous"] == 1

    def test_mixed_batch(self):
        outcomes = [
            EmailOutcome(
                message_id="a", date="", sender="hr@acme.com",
                subject="Offer", outcome="offer", confidence="high",
                matched_job_url="https://acme.com/jobs/1",
            ),
            EmailOutcome(
                message_id="b", date="", sender="hr@unknown.com",
                subject="Interview", outcome="interview", confidence="medium",
                matched_job_url=None,
            ),
            EmailOutcome(
                message_id="c", date="", sender="hr@globex.com",
                subject="Thanks", outcome="ambiguous", confidence="low",
                matched_job_url="https://globex.com/j",
            ),
        ]
        result = apply_outcomes(outcomes, dry_run=True)
        assert result["written"] == 1
        assert result["skipped_no_match"] == 1
        assert result["skipped_ambiguous"] == 1

    def test_apply_calls_record_application(self, monkeypatch):
        recorded = []

        def _fake_record(job_ref, status, channel, notes):
            recorded.append({"job_ref": job_ref, "status": status})

        import applypilot.applications as apps_module
        monkeypatch.setattr(apps_module, "record_application", _fake_record)

        # Patch the lazy import inside apply_outcomes
        import applypilot.gmail_outcomes as mod
        monkeypatch.setattr(mod, "_OUTCOME_TO_TRACKER", {"offer": "offer"})

        outcomes = [
            EmailOutcome(
                message_id="abc", date="Mon", sender="hr@acme.com",
                subject="Offer!", outcome="offer", confidence="high",
                matched_job_url="https://acme.com/jobs/1",
            ),
        ]

        # Patch record_application inside the function's lazy import scope
        import importlib, sys
        # Ensure record_application is patched at import site
        orig = sys.modules.get("applypilot.applications")
        try:
            import applypilot.applications as app_mod
            original_fn = app_mod.record_application
            app_mod.record_application = _fake_record
            result = apply_outcomes(outcomes, dry_run=False)
            assert result["written"] == 1
            assert recorded[0]["status"] == "offer"
        finally:
            app_mod.record_application = original_fn
