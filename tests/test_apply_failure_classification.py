from applypilot.apply.failure_classification import FailureEvidence, classify_apply_failure


def test_usage_limit_before_application_tools_is_safe_requeue():
    evidence = FailureEvidence(
        status="failed:no_result_line",
        transcript="You've hit your session limit. Switch to another model.",
        application_tool_calls=0,
        tool_calls_total=0,
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "usage_or_session_limit"
    assert result.safe_requeue is True


def test_short_usage_limit_before_application_tools_is_safe_requeue():
    evidence = FailureEvidence(
        status="failed:no_result_line",
        transcript="usage limit",
        application_tool_calls=0,
        tool_calls_total=0,
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "usage_or_session_limit"
    assert result.safe_requeue is True


def test_short_session_limit_before_application_tools_is_safe_requeue():
    evidence = FailureEvidence(
        status="failed:no_result_line",
        transcript="session limit",
        application_tool_calls=0,
        tool_calls_total=0,
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "usage_or_session_limit"
    assert result.safe_requeue is True


def test_usage_limit_after_browser_tool_is_not_safe_requeue():
    evidence = FailureEvidence(
        status="failed:no_result_line",
        transcript="You've hit your usage limit",
        application_tool_calls=2,
        tool_calls_total=2,
        last_tool="browser_click",
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "post_browser_no_result"
    assert result.safe_requeue is False


def test_mcp_start_failure_is_worker_level():
    evidence = FailureEvidence(
        status="failed:no_result_line",
        transcript="MCP startup failed: handshaking with MCP server failed",
        application_tool_calls=0,
        tool_calls_total=0,
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "mcp_start_failure"
    assert result.worker_level is True


def test_mcp_started_flag_false_is_worker_level():
    evidence = FailureEvidence(
        status="failed:no_result_line",
        transcript="",
        application_tool_calls=0,
        tool_calls_total=0,
        mcp_started_ok=False,
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "mcp_start_failure"
    assert result.worker_level is True


def test_weekly_limit_before_application_tools_is_safe_requeue():
    evidence = FailureEvidence(
        status="failed:no_result_line",
        transcript="You've hit your weekly limit. Try again at 8:00 AM.",
        application_tool_calls=0,
        tool_calls_total=0,
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "usage_or_session_limit"
    assert result.safe_requeue is True


def test_quota_signatures_before_application_tools_are_safe_requeue():
    transcripts = (
        "switch to a different model",
        "exceeded your quota",
        "insufficient quota",
        "out of credits",
        "upgrade to continue",
    )

    for transcript in transcripts:
        evidence = FailureEvidence(
            status="failed:no_result_line",
            transcript=transcript,
            application_tool_calls=0,
            tool_calls_total=0,
        )

        result = classify_apply_failure(evidence)

        assert result.failure_class == "usage_or_session_limit"
        assert result.safe_requeue is True


def test_auth_message_after_browser_tool_is_not_safe_requeue():
    evidence = FailureEvidence(
        status="failed:no_result_line",
        transcript="Invalid API key",
        application_tool_calls=1,
        tool_calls_total=1,
        last_tool="browser_click",
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "post_browser_no_result"
    assert result.safe_requeue is False


def test_none_status_and_transcript_are_malformed_result():
    evidence = FailureEvidence(status=None, transcript=None)

    result = classify_apply_failure(evidence)

    assert result.failure_class == "malformed_result"


def test_chrome_launch_failure_is_worker_level():
    evidence = FailureEvidence(
        status="failed:no_result_line",
        transcript="",
        chrome_launch_ok=False,
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "browser_launch_failure"
    assert result.worker_level is True


def test_cdp_connect_failure_is_worker_level():
    evidence = FailureEvidence(
        status="failed:no_result_line",
        transcript="",
        cdp_connect_ok=False,
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "cdp_lost"
    assert result.worker_level is True


def test_timeout_before_application_tools_is_worker_level_safe_requeue():
    evidence = FailureEvidence(
        status="failed:timeout",
        transcript="",
        application_tool_calls=0,
        tool_calls_total=0,
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "timeout"
    assert result.safe_requeue is True
    assert result.worker_level is True


def test_timeout_after_form_touch_is_unconfirmed():
    evidence = FailureEvidence(
        status="failed:timeout",
        transcript="",
        application_tool_calls=5,
        tool_calls_total=5,
        last_tool="browser_click",
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "post_form_crash_unconfirmed"
    assert result.safe_requeue is False


def test_zero_tool_no_result_is_distinct():
    evidence = FailureEvidence(
        status="failed:no_result_line",
        transcript="agent exited without result",
        application_tool_calls=0,
        tool_calls_total=0,
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "zero_tool_no_result"
