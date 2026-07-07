from applypilot.fleet.host_policy import HostPolicyDecision, decide_host_policy, host_from_url


def test_host_from_url_normalizes_host():
    assert host_from_url("https://Boards.Greenhouse.io/acme/jobs/1") == "boards.greenhouse.io"
    assert host_from_url("not a url") == ""
    assert host_from_url("https://not a url") == ""
    assert host_from_url("http://exa mple.com/path") == ""


def test_greenhouse_and_ashby_are_allowed_by_default():
    assert decide_host_policy("https://boards.greenhouse.io/acme/jobs/1").mode == "allow"
    assert decide_host_policy("https://jobs.ashbyhq.com/acme/1").mode == "allow"


def test_workday_is_supervised_by_default():
    decision = decide_host_policy("https://adobe.wd5.myworkdayjobs.com/external/job/1")

    assert decision == HostPolicyDecision(
        mode="supervised",
        reason="workday_tenant_requires_trust",
        host="adobe.wd5.myworkdayjobs.com",
        ats="workday",
    )


def test_trusted_workday_tenant_canary_overrides_default():
    decision = decide_host_policy(
        "https://adobe.wd5.myworkdayjobs.com/external/job/1",
        trusted_hosts={"adobe.wd5.myworkdayjobs.com": "canary"},
    )

    assert decision.mode == "canary"
    assert decision.reason == "trusted_host"


def test_known_low_yield_public_board_is_parked():
    decision = decide_host_policy("https://www.indeed.com/viewjob?jk=1")

    assert decision.mode == "supervised"
    assert decision.reason == "login_gate_prone"
