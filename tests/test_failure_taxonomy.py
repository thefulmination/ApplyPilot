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


def test_group_reason_counts_keeps_total_and_sorts_by_frequency():
    grouped = group_reason_counts({
        "failed:browser_unavailable": 3,
        "failed:browser_tool_unavailable": 2,
        "expired": 4,
    })
    assert grouped == {"browser_infrastructure": 5, "unavailable": 4}
    assert sum(grouped.values()) == 9
