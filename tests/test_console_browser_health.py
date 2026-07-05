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


def test_classify_employer_application_cap():
    c = B.classify_text("we limit the number of applications so our team can review")
    assert c["kind"] == "employer_application_cap"


def test_classify_usage_limit():
    c = B.classify_text("You've hit your session limit, resets 12:40pm")
    assert c["kind"] == "usage_limit"
