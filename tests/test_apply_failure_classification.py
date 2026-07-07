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
