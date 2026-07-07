from applypilot import aggregator_resolver


def test_source_platform_from_url():
    assert aggregator_resolver.source_platform_from_url("https://www.linkedin.com/jobs/view/123") == "linkedin"
    assert aggregator_resolver.source_platform_from_url("https://www.indeed.com/viewjob?jk=abc") == "indeed"
    assert aggregator_resolver.source_platform_from_url("https://jobs.ashbyhq.com/acme/123") is None


def test_shared_unresolved_taxonomy_maps_to_next_actions():
    assert aggregator_resolver.next_action_for_unresolved_kind("auth_required") == "refresh_session"
    assert aggregator_resolver.next_action_for_unresolved_kind("checkpoint_or_captcha") == "pause_resolver"
    assert aggregator_resolver.next_action_for_unresolved_kind("rate_limited") == "retry_later"
    assert aggregator_resolver.next_action_for_unresolved_kind("page_unreachable") == "retry_later"
    assert aggregator_resolver.next_action_for_unresolved_kind("dom_unreadable") == "retry_fresh_context"
    assert aggregator_resolver.next_action_for_unresolved_kind("apply_button_missing") == "run_ats_reconstruction"
    assert aggregator_resolver.next_action_for_unresolved_kind("conflicting_signals") == "run_conservative_dom_parser"
    assert aggregator_resolver.next_action_for_unresolved_kind("outbound_not_observed") == "retry_with_network_capture"
    assert aggregator_resolver.next_action_for_unresolved_kind("outbound_still_source_platform") == "run_url_unwrapper"
    assert aggregator_resolver.next_action_for_unresolved_kind("malformed_outbound_url") == "extract_from_serialized_page_data"
    assert aggregator_resolver.next_action_for_unresolved_kind("ats_reconstruction_needed") == "run_ats_reconstruction"
    assert aggregator_resolver.next_action_for_unresolved_kind("low_confidence_match") == "manual_review"


def test_external_apply_url_is_source_platform_aware():
    assert aggregator_resolver.is_external_apply_url("https://jobs.ashbyhq.com/acme/123") is True
    assert aggregator_resolver.is_external_apply_url("https://www.linkedin.com/jobs/view/123") is False
    assert aggregator_resolver.is_external_apply_url("https://www.indeed.com/viewjob?jk=abc") is False
    assert aggregator_resolver.is_external_apply_url(None) is False


def test_external_apply_url_rejects_invalid_url_shapes():
    assert aggregator_resolver.is_external_apply_url("http://[::1") is False
    assert aggregator_resolver.is_external_apply_url("mailto:jobs@example.com") is False
    assert aggregator_resolver.is_external_apply_url("https:///missing-host") is False
