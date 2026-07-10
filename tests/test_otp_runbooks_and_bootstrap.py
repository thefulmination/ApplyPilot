from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_m4_bootstrap_does_not_hydrate_gmail_mcp_credentials():
    bootstrap = (REPO / "bootstrap-m4.bat").read_text(encoding="utf-8")

    assert "hydrate-gmail.py" not in bootstrap
    assert "Gmail MCP" not in bootstrap
    assert not (REPO / "hydrate-gmail.py").exists()


def test_imap_runbook_canary_uses_mail_source_not_run_applypilot_wrapper():
    runbook = (REPO / "docs" / "imap-gmail-runbook.md").read_text(encoding="utf-8")

    assert ".\\run-applypilot.ps1 scan-gmail" not in runbook
    assert "from applypilot.mail_source import get_mail_source" in runbook
    assert "type(src).__name__" in runbook


def test_otp_relay_runbook_points_to_imap_app_password_secret():
    runbook = (REPO / "docs" / "fleet-otp-relay-runbook.md").read_text(encoding="utf-8")

    assert "gmail_app_password.json" in runbook
    assert "~/.applypilot/gmail_credentials.json" not in runbook


def test_otp_relay_runbook_covers_mission_grade_verification():
    runbook = (REPO / "docs" / "fleet-otp-relay-runbook.md").read_text(encoding="utf-8")

    assert "matched_message_id" in runbook
    assert "otp_delivery_stalled" in runbook
    assert "X-GM-RAW" in runbook
    assert "controlled end-to-end" in runbook


def test_otp_relay_controlled_cycle_uses_runtime_configuration_and_health_probe():
    runbook = (REPO / "docs" / "fleet-otp-relay-runbook.md").read_text(encoding="utf-8")

    assert "$env:FLEET_PG_DSN =" in runbook
    assert "$env:APPLYPILOT_FLEET_DSN =" not in runbook
    assert "gmail_token_ok=True" not in runbook
    assert "deadman.mail_source_alive()" in runbook


def test_otp_relay_controlled_cycle_lists_exact_approved_evidence():
    runbook = (REPO / "docs" / "fleet-otp-relay-runbook.md").read_text(encoding="utf-8")

    for fact in (
        "request_created=yes",
        "responder_answered=yes",
        "worker_consumed=yes",
        "code_cleared=yes",
        "matched_message_id_retained=yes",
        "assisted_retry_terminal=yes",
        "deadman_otp_alerts=0",
    ):
        assert fact in runbook
