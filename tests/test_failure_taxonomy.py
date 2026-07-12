from applypilot.fleet.failure_taxonomy import canonical_failure_group, group_reason_counts


def test_canonical_failure_group_collapses_browser_aliases():
    aliases = (
        "failed:browser_unavailable",
        "failed:browser_tool_unavailable",
        "failed:playwright_server_crashed",
        "failed:browser_mcp_disconnected",
    )
    assert {canonical_failure_group(reason) for reason in aliases} == {"browser_infrastructure"}


def test_canonical_failure_group_preserves_distinct_operator_actions():
    assert canonical_failure_group("expired") == "unavailable"
    assert canonical_failure_group("dedup:already_applied") == "duplicate_or_already_applied"
    assert canonical_failure_group("failed:not_eligible_location") == "eligibility"
    assert canonical_failure_group("failed:email_verification_required") == "access_or_verification"
    assert canonical_failure_group("adapter_unsupported") == "routing_or_policy"
    assert canonical_failure_group("requeued_by_autotriage:pre_touch_crash") == "remediation_history"
    assert canonical_failure_group("failed:usage_limit") == "provider_usage_limit"
    assert canonical_failure_group("email_reconcile_review_required") == "manual_review"
    assert canonical_failure_group("failed:suspicious_page") == "page_or_content_failure"
    assert canonical_failure_group("challenge_skipped") == "operator_skipped"
    assert canonical_failure_group("stale_unapproved") == "retired_unapproved"
    assert canonical_failure_group("canonical_provenance_missing") == "retired_unapproved"
    assert canonical_failure_group("failed:reason") == "malformed_failure_reason"
    assert canonical_failure_group("failed:unspecified") == "malformed_failure_reason"
    assert canonical_failure_group("unsafe_prior_attempt") == "submission_uncertain"
    assert canonical_failure_group("submission_uncertain:dead_posting") == "submission_uncertain"
    assert canonical_failure_group(
        "75 application tool calls with no confirmed applied event or inbox outcome; "
        "manual review required; unsafe to retry"
    ) == "submission_uncertain"
    assert canonical_failure_group(
        "zero application tool calls but attempts=99; unsafe to retry; outcome unresolved"
    ) == "submission_uncertain"
    assert canonical_failure_group("failed:not_a_job_application") == "not_an_application"
    assert canonical_failure_group("failed:no_resume_content_access_required_specifics") == "missing_required_material"
    assert canonical_failure_group("failed:not_a_paid_job") == "job_type_excluded"
    assert canonical_failure_group("failed:not_a_salaried_position") == "job_type_excluded"
    assert canonical_failure_group("failed:required_gender_field_has_no_decline_option") == "form_or_profile_constraint"
    assert canonical_failure_group("failed:role_requires_jd_and_bar_license_not_supported_by_profile") == "eligibility"
    assert canonical_failure_group("failed:site_error_epam_backend_unavailable") == "page_or_content_failure"


def test_group_reason_counts_keeps_total_and_sorts_by_frequency():
    grouped = group_reason_counts({
        "failed:browser_unavailable": 3,
        "failed:browser_tool_unavailable": 2,
        "expired": 4,
    })
    assert grouped == {"browser_infrastructure": 5, "unavailable": 4}
    assert sum(grouped.values()) == 9
