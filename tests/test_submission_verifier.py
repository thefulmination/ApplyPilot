from applypilot.apply.submission_verifier import SubmissionEvidence, verify_submission


def test_allowlisted_success_response_with_identifier_is_verified():
    result = verify_submission(
        SubmissionEvidence(
            response_ok=True,
            response_id="app-123",
            response_url="https://boards-api.greenhouse.io/v1/applications",
            allowed_response_hosts=("boards-api.greenhouse.io",),
        )
    )

    assert result.status == "verified"
    assert result.method == "response_id"
    assert result.reference == "app-123"


def test_known_success_url_requires_confirmation_dom():
    result = verify_submission(
        SubmissionEvidence(
            page_url="https://job-boards.greenhouse.io/acme/jobs/1/confirmation",
            allowed_success_hosts=("job-boards.greenhouse.io",),
            success_url_markers=("/confirmation",),
            dom_text="Thank you for applying. Your application was submitted.",
        )
    )

    assert result.status == "verified"
    assert result.method == "success_url_dom"


def test_confirmation_dom_is_verified_without_success_url():
    result = verify_submission(
        SubmissionEvidence(dom_text="Your application has been submitted successfully")
    )

    assert result.status == "verified"
    assert result.method == "confirmation_dom"


def test_matched_confirmation_email_is_verified():
    result = verify_submission(
        SubmissionEvidence(confirmation_email_ref="gmail:message-42")
    )

    assert result.status == "verified"
    assert result.method == "confirmation_email"


def test_validation_error_is_contradicted_without_stronger_response_evidence():
    result = verify_submission(
        SubmissionEvidence(
            dom_text="Please correct the highlighted fields",
            validation_errors=("Phone number is required",),
        )
    )

    assert result.status == "contradicted"
    assert result.method == "validation_error"


def test_screenshot_and_disabled_button_are_supporting_evidence_only():
    result = verify_submission(
        SubmissionEvidence(screenshot_present=True, submit_button_disabled=True)
    )

    assert result.status == "unverified"
    assert result.method is None


def test_unallowlisted_response_identifier_is_not_verified():
    result = verify_submission(
        SubmissionEvidence(
            response_ok=True,
            response_id="app-123",
            response_url="https://lookalike.example/applications",
            allowed_response_hosts=("boards-api.greenhouse.io",),
        )
    )

    assert result.status == "unverified"

