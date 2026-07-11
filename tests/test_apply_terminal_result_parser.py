from applypilot.apply.launcher import _parse_terminal_result


def test_narrative_applied_mention_does_not_override_later_failure():
    text = """Given the hard rule to never claim RESULT:APPLIED without observed
confirmation, I am stopping here.

RESULT:FAILED:budget_exhausted
"""
    assert _parse_terminal_result(text) == "failed:budget_exhausted"


def test_only_standalone_result_lines_are_terminal():
    assert _parse_terminal_result("Never claim RESULT:APPLIED without proof.") is None


def test_last_standalone_terminal_line_wins():
    text = "RESULT:FAILED:validation\nRESULT:APPLIED\n"
    assert _parse_terminal_result(text) == "applied"


def test_markdown_wrapped_terminal_line_is_supported():
    assert _parse_terminal_result("**RESULT:EXPIRED**") == "expired"


def test_failed_reason_is_cleaned_and_normalized():
    assert _parse_terminal_result("`RESULT:FAILED:No_Confirmation`") == "failed:no_confirmation"


def test_known_non_failure_statuses_are_normalized():
    assert _parse_terminal_result("RESULT:DRY_RUN") == "dry_run"
    assert _parse_terminal_result("RESULT:AUTH_REQUIRED") == "auth_required"


def test_unknown_result_code_is_not_terminal():
    assert _parse_terminal_result("RESULT:MAYBE") is None
