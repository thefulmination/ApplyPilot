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

    def test_application_receipt_is_acknowledged_not_ambiguous(self):
        # An application receipt is now its OWN category (was 'ambiguous', and worse,
        # often leaked into 'interview' via generic "next steps" body phrases).
        outcome, _, _ = classify_email_outcome(
            subject="We received your application",
            body="Thank you for applying. We will be in touch.",
        )
        assert outcome == "acknowledged"

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


class TestNonJobGate:
    """Promotional / financial / newsletter emails must be dropped, not classified.
    These are real shapes the broad gmail query pulls in (the loan-"offer" that used to
    be flagged as a job 'offer')."""

    @pytest.mark.parametrize("subject,body,sender", [
        ("You have a Citi Personal Loan offer of $40,000",
         "Apply now for a Citi Personal Loan. View in browser.", "offers@info15.citi.com"),
        ("Your balance transfer offer ends soon",
         "Transfer a balance and save on interest. View in browser. Unsubscribe.",
         "no-reply@info15.citi.com"),
        ("Jonathan, following up on your student loan refi offer",
         "Refinance your student loan today.", "hello@hello.earnest.com"),
        ("Get a $0 Companion Fare offer to use toward your next trip",
         "And a 60000 bonus point offer.", "deals@email.alaskaair.com"),
        ("Athletic Shoes: Dick's Sporting Goods In-App Offer",
         "This deal matches your alert.", "alerts@da.slickdeals.net"),
        ("Medium daily digest", "Today's highlights for you.", "noreply@medium.com"),
    ])
    def test_promotional_emails_dropped(self, subject, body, sender):
        outcome, _, _ = classify_email_outcome(subject, body, sender)
        assert outcome == "not_job"

    def test_loan_offer_letter_is_not_a_job_offer(self):
        # The exact false positive that started this: a finance "offer letter" must NOT
        # be classified as a job offer.
        outcome, _, _ = classify_email_outcome(
            subject="Your pre-selected personal loan offer letter",
            body="View this offer in browser. Annual percentage rate applies. Unsubscribe.",
            sender="loans@firsttechfed.com",
        )
        assert outcome != "offer"
        assert outcome == "not_job"

    def test_real_job_offer_survives_the_gate(self):
        outcome, _, _ = classify_email_outcome(
            subject="Your job offer from Globex",
            body="We would like to offer you the position of Chief of Staff. "
                 "Your annual base salary of $180,000.",
            sender="people@globex.com",
        )
        assert outcome == "offer"


class TestAcknowledgedVsInterview:
    """The core fix: application receipts are 'acknowledged', not 'interview'."""

    @pytest.mark.parametrize("subject,body", [
        ("Thank you for applying to Socure!",
         "Hi Jonathan, Thank you for applying to the Chief of Staff opening. "
         "Your application has been received and we will review it."),
        ("Thanks for applying to Exa",
         "We've received your application and will be in touch about next steps."),
        ("Dyna Robotics Application Confirmation",
         "Thank you for applying to Dyna! We've received your application. "
         "We appreciate your patience and wish you the best in your job search."),
        ("We've received your application to Nooks!",
         "Thanks Jonathan! We received your application and will review every candidate."),
    ])
    def test_receipts_are_acknowledged(self, subject, body):
        outcome, _, _ = classify_email_outcome(subject, body, sender="no-reply@us.greenhouse-mail.io")
        assert outcome == "acknowledged"

    def test_receipt_with_boilerplate_next_steps_not_interview(self):
        # "we'll be in touch about next steps" is boilerplate, NOT an interview request.
        outcome, _, _ = classify_email_outcome(
            subject="Thank you for applying to Acme",
            body="Your application has been received. We'll be in touch about next steps.",
        )
        assert outcome == "acknowledged"

    def test_genuine_interview_request_still_wins(self):
        # A real interview request beats a generic receipt subject.
        outcome, _, _ = classify_email_outcome(
            subject="Next steps for your application",
            body="We would like to move forward. Please schedule a 30-minute video interview.",
        )
        assert outcome == "interview"


class TestConditionalRejectionIsNotRejection:
    """ATS acknowledgment boilerplate contains conditional/courtesy phrases that must NOT
    register as a rejection (the 5 false positives found on the live inbox)."""

    def test_if_not_selected_boilerplate_is_acknowledged(self):
        # The Lever acknowledgment template (Formic/Xsolla/Hey Jane).
        outcome, _, _ = classify_email_outcome(
            subject="Thank you for applying to Formic",
            body="Thanks for applying. If you are not selected for this position, "
                 "please keep an eye on our careers page for future openings.",
            sender="no-reply@hire.lever.co",
        )
        assert outcome == "acknowledged"

    def test_wish_you_the_best_courtesy_is_acknowledged(self):
        outcome, _, _ = classify_email_outcome(
            subject="Application Confirmation",
            body="Thank you for applying! We've received your application. "
                 "We wish you the best in your job search.",
            sender="no-reply@ashbyhq.com",
        )
        assert outcome == "acknowledged"

    def test_definite_rejection_overrides_receipt(self):
        # A real rejection phrased as "thanks for applying ... but we've decided to
        # pursue other candidates" must still be a rejection.
        outcome, _, _ = classify_email_outcome(
            subject="Your application Business Operations Coordinator",
            body="Thank you for applying. After reviewing your application we have "
                 "decided to pursue other candidates whose experience more closely aligns.",
            sender="no-reply@myworkday.com",
        )
        assert outcome == "rejected"


class TestAtsMailDomains:
    """ATS platforms send from *-mail.* subdomains (greenhouse-mail.io, teamtailor-mail
    .com), not the bare board domain -- detection/matching used to miss every one."""

    def test_greenhouse_mail_subdomain_is_ats(self):
        from applypilot.gmail_outcomes import _is_ats_domain
        assert _is_ats_domain("us.greenhouse-mail.io") is True
        assert _is_ats_domain("revalue.teamtailor-mail.com") is True
        assert _is_ats_domain("ats.rippling.com") is True
        assert _is_ats_domain("app.bamboohr.com") is True
        assert _is_ats_domain("gmail.com") is False

    def test_ats_sender_is_always_job_related(self):
        from applypilot.gmail_outcomes import is_non_job_email
        # Even a terse ATS email with no anchors is a job email (not dropped).
        non_job, _ = is_non_job_email("Update", "Please see the portal.",
                                      "noreply@us.greenhouse-mail.io")
        assert non_job is False


class TestAdversarialBoundaries:
    """Regression guards for the 18 cases an adversarial review surfaced (offers/
    interviews/rejections that the live all-acknowledgment inbox could not exercise).
    The unifying rule: only a CONCRETE signal overrides an acknowledgment receipt;
    receipt boilerplate ("if your background matches, we'll reach out to schedule a
    call") must NOT register as a real interview/rejection."""

    @pytest.mark.parametrize("subject,body,sender,expected", [
        # --- precision: promos that reuse job words must drop ---
        ("Your offer is ready",
         "Your application for a loan is approved. We would like to offer you 0 percent APR.",
         "noreply@bankpromo-mail.com", "not_job"),
        ("Schedule your free career coaching call",
         "Schedule a call with a coach. Pick a time on calendly.",
         "hello@careercoachpro-news.com", "not_job"),
        ("We received your application for a new credit card",
         "Thank you for applying. We have received your application.",
         "noreply@cardservices-mail.com", "not_job"),
        # --- recall: real outcomes from NON-ATS company senders must survive the gate ---
        ("Congratulations from Initech", "Welcome you aboard. Compensation: 170000. Start date: August 1.",
         "careers@initech.com", "offer"),
        ("Unfortunately, an update from Acme",
         "We regret to inform you that you have not been selected.", "talent@acme.com", "rejected"),
        # --- neutral status subjects must NOT inject a rejection ---
        ("An update on your application",
         "Congratulations! We would like to offer you the role. We are thrilled to have you join the team.",
         "careers@acme.com", "offer"),
        ("Update on your application",
         "Great news - we'd like to move you forward to the next stage. Someone will be in touch shortly.",
         "talent@acme.com", "acknowledged"),
        # --- buried interview/assessment under a receipt subject -> interview ---
        ("We've received your application",
         "Thanks for applying. Please complete the online assessment via HackerRank.",
         "noreply@hackerrank-mail.com", "interview"),
        ("Application received", "We received your application. Are you available for a phone interview?",
         "hr@company.com", "interview"),
        # --- soft rejection under a receipt subject -> rejected ---
        ("Thank you for applying",
         "Thank you for applying. After review, you are not the right fit for this role.",
         "careers@company.com", "rejected"),
        ("Thanks for applying", "Thank you for applying. We have chosen another candidate for this role.",
         "hr@company.com", "rejected"),
        # --- receipt boilerplate (conditional/future) must STAY acknowledged ---
        ("Thank you for applying to Acme",
         "Thanks for applying. If your background aligns, we will reach out to schedule an interview.",
         "no-reply@us.greenhouse-mail.io", "acknowledged"),
        ("Thank you for applying",
         "Thank you for applying. Due to the volume of applications, we unfortunately cannot respond to everyone.",
         "hr@company.com", "acknowledged"),
        # --- thread-title interview subject on a post-interview rejection -> rejected ---
        ("Your interview with Acme Corp",
         "Thank you for interviewing. Unfortunately, we have decided to go with another candidate.",
         "recruiting@acmecorp.com", "rejected"),
    ])
    def test_boundary_case(self, subject, body, sender, expected):
        outcome, _, _ = classify_email_outcome(subject, body, sender)
        assert outcome == expected, f"{subject!r} -> {outcome}, expected {expected}"


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


class TestCompanyExtraction:
    """Company extraction must handle the dominant real ATS subject shapes -- the old
    extractor only knew 'application/applied' and missed every 'applying to <Company>',
    which is ~75% of receipts."""

    @pytest.mark.parametrize("subject,expected", [
        ("Thank you for applying to Justworks", "Justworks"),
        ("Thank You for Applying to ServiceNow!", "ServiceNow"),
        ("Thanks for applying to Exa 🚀", "Exa"),                  # emoji stripped
        ("Thank you for applying at UpEquity", "UpEquity"),
        ("AeroVect — Thanks for Applying!", "AeroVect"),           # company-first (em dash)
        ("RZR Global Inc. | Thank you for applying", "RZR Global Inc"),  # company-first pipe
        ("Crusoe | Application Received", "Crusoe"),
        ("Novo Nordisk: Thank You for Your Application", "Novo Nordisk"),  # colon
        ("Thank you for applying to Formic │ Chief of Staff", "Formic"),   # role tail dropped
        ("Thanks for your interest in Carta, Jonathan", "Carta"),
        ("Your application at Acme Corp for Chief of Staff", "Acme Corp"),  # stop at " for"
        ("Interview with Globex for Strategy Manager", "Globex"),
    ])
    def test_extract_company(self, subject, expected):
        from applypilot.gmail_outcomes import _extract_company_from_subject
        assert _extract_company_from_subject(subject) == expected

    @pytest.mark.parametrize("subject", [
        "Jonathan, we've received your application",   # greeting must not be captured
        "Thanks for applying!",                        # no company present
        "We've Received Your Application | January",    # boilerplate, not a company
    ])
    def test_no_false_company(self, subject):
        from applypilot.gmail_outcomes import _extract_company_from_subject
        got = _extract_company_from_subject(subject)
        assert got is None or got.lower() not in ("jonathan", "we've", "we", "your")


class TestBoardSlugMatching:
    """ATS board-slug from links in the email = exact employer match (no fuzzy guessing)."""

    def test_board_slug_extraction(self):
        from applypilot.gmail_outcomes import _board_slugs
        slugs = _board_slugs("View it here: https://job-boards.greenhouse.io/justworks/jobs/123")
        assert "greenhouse.io/justworks" in slugs

    def test_board_slug_matches_applied_job(self):
        jobs = [{"url": "https://hiring.cafe/x", "title": "Chief of Staff",
                 "application_url": "https://job-boards.greenhouse.io/justworks/jobs/9",
                 "company": "Justworks", "site": "Justworks"}]
        job, method, score = match_email_to_job(
            sender="no-reply@us.greenhouse-mail.io",
            subject="Thanks for applying!",   # no company in subject
            body="We received it. Track it at https://boards.greenhouse.io/justworks/jobs/9",
            applied_jobs=jobs,
        ).astuple()
        assert job is not None and method == "board_slug"


class TestLinkedIn:
    """LinkedIn Easy Apply confirmations ('your application was sent to X') -- recognized
    as acknowledgments, company extracted, and matched by exact job id."""

    def test_application_was_sent_is_acknowledged(self):
        outcome, _, _ = classify_email_outcome(
            "Jonathan, your application was sent to Startup Resources",
            "View your application on LinkedIn.", "jobs-noreply@linkedin.com")
        assert outcome == "acknowledged"

    def test_company_extracted_from_application_was_sent(self):
        from applypilot.gmail_outcomes import _extract_company_from_subject
        assert _extract_company_from_subject(
            "Jonathan, your application was sent to Jobot") == "Jobot"

    def test_linkedin_job_id_extraction(self):
        from applypilot.gmail_outcomes import _linkedin_job_ids
        ids = _linkedin_job_ids("see https://www.linkedin.com/jobs/view/4423505078/ and "
                                "https://linkedin.com/jobs/collections/x?currentJobId=999888777")
        assert ids == {"4423505078", "999888777"}

    def test_linkedin_job_id_exact_match(self):
        jobs = [{"url": "https://www.linkedin.com/jobs/view/4423505078", "title": "Chief of Staff",
                 "application_url": None, "company": "Startup Resources", "site": "LinkedIn"}]
        job, method, score = match_email_to_job(
            sender="jobs-noreply@linkedin.com",
            subject="Jonathan, your application was sent to Startup Resources",
            body="Track it: https://www.linkedin.com/jobs/view/4423505078/",
            applied_jobs=jobs).astuple()
        assert job is not None and method == "linkedin_job_id"


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
        ).astuple()
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
        ).astuple()
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
        ).astuple()
        assert job is not None
        assert "Acme" in (job.get("company") or job.get("site") or "")

    def test_ats_sender_uses_company_from_subject(self):
        job, method, _ = match_email_to_job(
            sender="noreply@greenhouse.io",
            subject="Interview with Globex for Strategy Manager",
            body="",
            applied_jobs=SAMPLE_JOBS,
        ).astuple()
        assert job is not None
        assert job["site"] == "Globex"
        assert method == "ats_domain"

    def test_no_match_when_unrelated(self):
        job, method, score = match_email_to_job(
            sender="newsletter@someotherdomain.com",
            subject="Weekly digest",
            body="Your weekly newsletter from...",
            applied_jobs=SAMPLE_JOBS,
        ).astuple()
        assert job is None
        assert method is None

    def test_empty_job_list(self):
        job, method, score = match_email_to_job(
            sender="hr@acme.com",
            subject="Interview",
            body="",
            applied_jobs=[],
        ).astuple()
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

    def test_acknowledged_is_shown_but_not_written(self):
        # Acknowledgments are receipts -- counted/displayed but never written to the
        # tracker (the tool already recorded the apply; an old receipt must not downgrade
        # a job that has since advanced).
        outcomes = [
            EmailOutcome(
                message_id="ack", date="", sender="no-reply@us.greenhouse-mail.io",
                subject="Thank you for applying", outcome="acknowledged", confidence="high",
                matched_job_url="https://acme.com/jobs/1",
            ),
        ]
        result = apply_outcomes(outcomes, dry_run=True)
        assert result["written"] == 0
        assert result["skipped_acknowledged"] == 1

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


# ---------------------------------------------------------------------------
# Temporal guard (audit 2026-07-02: 2/26 rejections provably predate their apply)
# ---------------------------------------------------------------------------
from applypilot.gmail_outcomes import MatchResult


class TestTemporalGuard:
    def _job(self, **kw):
        base = {"url": "https://boards.greenhouse.io/checkr/jobs/1",
                "application_url": "https://boards.greenhouse.io/checkr/jobs/1",
                "title": "Analyst", "site": "Checkr", "company": "Checkr",
                "applied_at": "2026-06-28T12:00:00+00:00"}
        base.update(kw)
        return base

    def test_email_predating_application_is_quarantined(self):
        r = match_email_to_job(
            "no-reply@us.greenhouse-mail.io",
            "Your application to Checkr",
            "Thank you for applying to Checkr. Unfortunately...",
            [self._job()],
            occurred_at="2026-06-20T12:00:00+00:00",   # 8 days BEFORE applied_at
        )
        assert isinstance(r, MatchResult)
        assert r.job is None
        assert r.status == "needs_review"
        assert r.reason == "predates_application"

    def test_email_within_grace_passes(self):
        # acknowledgment 5 minutes BEFORE applied_at stamp (clock skew) still matches
        r = match_email_to_job(
            "no-reply@us.greenhouse-mail.io",
            "Your application to Checkr",
            "Thank you for applying to Checkr.",
            [self._job()],
            occurred_at="2026-06-28T11:55:00+00:00",
        )
        assert r.status == "attributed"
        assert r.job is not None

    def test_exact_board_slug_is_also_guarded(self):
        # spec: the guard applies to EVERY tier, exact ones included
        job = self._job()
        r = match_email_to_job(
            "no-reply@us.greenhouse-mail.io",
            "Update",
            "See https://boards.greenhouse.io/checkr/jobs/1",
            [job],
            occurred_at="2026-06-01T00:00:00+00:00",
        )
        assert r.status == "needs_review" and r.reason == "predates_application"

    def test_missing_occurred_at_with_guarded_candidates_quarantines(self):
        r = match_email_to_job(
            "no-reply@us.greenhouse-mail.io", "Your application to Checkr",
            "Thanks for applying to Checkr.", [self._job()], occurred_at=None,
        )
        assert r.status == "needs_review" and r.reason == "no_timestamp"

    def test_no_timestamps_anywhere_stays_eligible(self):
        # candidates without applied_at/guard_after are judged as before (back-compat)
        job = self._job(); del job["applied_at"]
        r = match_email_to_job(
            "no-reply@us.greenhouse-mail.io", "Your application to Checkr",
            "Thanks for applying to Checkr.", [job], occurred_at=None,
        )
        assert r.status == "attributed" and r.job is not None

    def test_guard_after_is_honored_for_crash_candidates(self):
        job = self._job(); del job["applied_at"]; job["guard_after"] = "2026-06-28T12:00:00+00:00"
        r = match_email_to_job(
            "no-reply@us.greenhouse-mail.io", "Your application to Checkr",
            "Thanks for applying to Checkr.", [job],
            occurred_at="2026-06-20T12:00:00+00:00",
        )
        assert r.status == "needs_review" and r.reason == "predates_application"
