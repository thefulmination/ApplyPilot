from applypilot.fleet import emergency_admission


def test_crash_liveness_is_allowed_during_browser_authority_hold():
    result = emergency_admission.apply_home_admission("crash-liveness")

    assert result.allowed is True
    assert "read-only apply-home command" in result.reason
