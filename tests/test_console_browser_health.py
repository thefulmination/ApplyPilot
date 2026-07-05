import pytest

from applypilot.fleet import console_browser_health as B


def test_classify_browser_backend_crash():
    c = B.classify_text("The Playwright browser backend at port 9401 has crashed and is unavailable")
    assert c["kind"] == "browser_backend_crashed"
    assert c["severity"] == "error"


def test_classify_browser_connection_refused():
    c = B.classify_text("ECONNREFUSED on port 9400; browser_unavailable")
    assert c["kind"] == "browser_service_unavailable"


def test_classify_captcha():
    c = B.classify_text("hCaptcha appeared and CapSolver returned ERROR_INVALID_TASK_DATA")
    assert c["kind"] == "captcha"


def test_classify_login_gate():
    c = B.classify_text("RESULT:AUTH_REQUIRED login page detected")
    assert c["kind"] == "login_gate"


@pytest.mark.parametrize(
    "text",
    [
        "parked challenge https://x (login_gate->owner_tray)",
        "RESULT:FAILED:login_issue",
    ],
)
def test_classify_login_gate_live_aliases(text):
    c = B.classify_text(text)
    assert c["kind"] == "login_gate"
    assert c["severity"] == "warn"


@pytest.mark.parametrize(
    "text",
    [
        "parked challenge https://x (email_otp->auto_otp)",
        "RESULT:FAILED:email_verification_required",
        "Please enter the security code sent to your email.",
    ],
)
def test_classify_email_otp_live_aliases(text):
    c = B.classify_text(text)
    assert c["kind"] == "email_otp"
    assert c["severity"] == "warn"


def test_classify_employer_application_cap():
    c = B.classify_text("we limit the number of applications so our team can review")
    assert c["kind"] == "employer_application_cap"


def test_classify_usage_limit():
    c = B.classify_text("You've hit your session limit, resets 12:40pm")
    assert c["kind"] == "usage_limit"


@pytest.mark.parametrize(
    "text",
    [
        "You've hit your usage limit. Try again later.",
        "Usage limit reached for this account",
    ],
)
def test_classify_usage_limit_aliases(text):
    c = B.classify_text(text)
    assert c["kind"] == "usage_limit"
    assert c["severity"] == "warn"


@pytest.mark.parametrize(
    "text",
    [
        "browser_backend_crashed while launching worker browser",
        "RESULT:browser_crashed after navigation",
    ],
)
def test_classify_browser_backend_crash_aliases(text):
    c = B.classify_text(text)
    assert c["kind"] == "browser_backend_crashed"
    assert c["severity"] == "error"


def test_classify_browser_server_unavailable_spaced_alias():
    c = B.classify_text("browser server unavailable for worker m4-2")
    assert c["kind"] == "browser_server_unavailable"
    assert c["severity"] == "error"


def test_summarize_worker_logs_counts_examples_and_skips_unknown():
    summary = B.summarize_worker_logs([
        {
            "worker_id": "w1",
            "machine_owner": "m4",
            "last_error": "browser_backend_crashed",
            "recent_log": "",
        },
        {
            "worker_id": "w2",
            "machine_owner": "home",
            "last_error": "",
            "recent_log": "ordinary heartbeat",
        },
    ])

    assert summary["counts"] == {"browser_backend_crashed": 1}
    assert summary["examples"] == {
        "browser_backend_crashed": {
            "worker_id": "w1",
            "machine_owner": "m4",
            "severity": "error",
        }
    }
