from __future__ import annotations

from applypilot.apply.launcher import _is_auth_required_result
from applypilot.applications import _normalize_status


def test_auth_required_result_detection() -> None:
    assert _is_auth_required_result("auth_required")
    assert _is_auth_required_result("failed:sso_required")
    assert _is_auth_required_result("email_verification_required")
    assert not _is_auth_required_result("failed:page_error")


def test_application_status_aliases_for_assisted_flow() -> None:
    assert _normalize_status("login required") == "auth_required"
    assert _normalize_status("2fa-required") == "auth_required"
    assert _normalize_status("manual required") == "assisted"
