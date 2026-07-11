import re
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _otp_runbook() -> str:
    return (REPO / "docs" / "fleet-otp-relay-runbook.md").read_text(encoding="utf-8")


def _fenced_blocks(text: str, language: str) -> list[str]:
    return re.findall(rf"```{language}\s*\n(.*?)```", text, flags=re.DOTALL)


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


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
        "inbox_auth_prearmed=yes",
        "assisted_retry_count=1",
        "assisted_retry_terminal=yes",
        "deadman_otp_alerts=0",
    ):
        assert fact in runbook


def test_otp_relay_restart_preserves_supervisor_and_scopes_child_to_checkout():
    runbook = _otp_runbook()
    restart = _section(
        runbook,
        "## Start and restart only the responder",
        "## Privacy-safe Gmail canary",
    )

    assert '$Launcher = (Resolve-Path -LiteralPath ".\\run-otp-responder.ps1").Path' in restart
    assert "$_.CommandLine.IndexOf($Launcher" in restart
    assert "if ($Supervisor.Count -eq 1)" in restart
    assert '"-Supervise"' in restart
    assert "Start-Process -FilePath \"powershell.exe\"" in restart
    assert "Stop-Process -Id $Supervisor" not in restart
    assert "$_.ExecutablePath -eq $ExpectedResponderExe" in restart
    assert '$_.Name -eq "applypilot-fleet-otp-home.exe"' not in restart
    assert restart.count("Stop-Process -Id $_.ProcessId") == 1


def test_otp_relay_controlled_cycle_is_unique_and_restores_environment():
    runbook = _otp_runbook()
    controlled = _section(
        runbook,
        "## Controlled end-to-end acceptance",
        "Acceptance requires these exact non-secret facts:",
    )

    assert '"otp-e2e-home-$([guid]::NewGuid().ToString(\'N\'))"' in controlled
    assert "& {" in controlled
    assert "try {" in controlled
    assert "finally {" in controlled
    assert "$PreviousEnvironment" in controlled
    assert "[Environment]::SetEnvironmentVariable($Name" in controlled
    assert 'Remove-Item -LiteralPath "Env:$Name"' in controlled
    assert ".\\.conda-env\\python.exe -" in controlled
    for name in (
        "FLEET_WORKER_ID",
        "APPLYPILOT_INBOX_AUTH",
        "APPLYPILOT_INBOX_AUTH_MODE",
        "FLEET_PG_DSN",
        "OTP_E2E_STARTED_AT",
        "APPLYPILOT_OTP_E2E_SLOT",
        "APPLYPILOT_OTP_E2E_URL",
    ):
        assert f'"{name}"' in controlled


def test_otp_relay_controlled_cycle_uses_production_prearm_and_automatic_evidence():
    runbook = _otp_runbook()
    controlled = _section(
        runbook,
        "## Controlled end-to-end acceptance",
        "Acceptance requires these exact non-secret facts:",
    )

    assert "from applypilot.fleet import apply_worker_main" in controlled
    assert "apply_worker_main._setup_apply_env()" in controlled
    assert "apply_worker_main.make_apply_fn(" in controlled
    assert "fleet_worker_id=worker_id" in controlled
    assert "(job)" in controlled
    assert "APPLYPILOT_OTP_E2E_SLOT" in controlled
    assert '"9400"' in controlled
    assert "APPLYPILOT_BASE_CDP_PORT" in controlled
    assert 'result["assisted_retry_count"]' in controlled
    assert 'result["assisted_retry_terminal"]' in controlled
    assert 'result["inbox_auth_prearmed"]' in controlled
    assert "contextlib.redirect_stdout(sink)" in controlled
    assert "contextlib.redirect_stderr(sink)" in controlled
    assert "$TerminalResult" not in controlled
    assert "Did exactly one assisted retry" not in controlled
    assert "run-applypilot.ps1" not in controlled
    assert "apply --url" not in controlled
    assert "print(result" not in controlled
    assert "run_status" not in controlled
    for field in (
        '"url"',
        '"application_url"',
        '"title"',
        '"company"',
        '"site"',
        '"score"',
        '"fit_score"',
        '"description"',
        '"source"',
        '"tailored_resume_path"',
    ):
        assert field in controlled


def test_otp_relay_controlled_cycle_fails_closed_on_unknown_mail_health():
    runbook = _otp_runbook()
    controlled = _section(
        runbook,
        "## Controlled end-to-end acceptance",
        "Acceptance requires these exact non-secret facts:",
    )

    assert "mail_source_ok is True" in controlled
    assert "otp_alert_count = max(1," in controlled
    assert "deadman_otp_alerts={otp_alert_count}" in controlled
    assert "mail_source_ok=" not in controlled


def test_otp_relay_runbook_powershell_and_embedded_python_parse():
    runbook = _otp_runbook()
    powershell = "\n".join(_fenced_blocks(runbook, "powershell"))
    parser = (
        "$tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseInput("
        "[Console]::In.ReadToEnd(),[ref]$tokens,[ref]$errors) | Out-Null; "
        "if($errors.Count){$errors | ForEach-Object {$_.Message}; exit 1}"
    )
    parsed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", parser],
        input=powershell,
        text=True,
        capture_output=True,
        check=False,
    )
    assert parsed.returncode == 0, parsed.stdout + parsed.stderr

    snippets = re.findall(
        r"@'\s*\n(.*?)\n'@\s*\|\s*\.\\\.conda-env\\python\.exe\s+-",
        runbook,
        flags=re.DOTALL,
    )
    assert snippets
    for snippet in snippets:
        compile(snippet, "<fleet-otp-relay-runbook>", "exec")
