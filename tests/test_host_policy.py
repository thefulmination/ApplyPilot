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


def test_trusted_host_keys_are_normalized():
    decision = decide_host_policy(
        "https://adobe.wd5.myworkdayjobs.com/external/job/1",
        trusted_hosts={"ADOBE.WD5.MYWORKDAYJOBS.COM": "canary"},
    )

    assert decision.mode == "canary"
    assert decision.reason == "trusted_host"


def test_trusted_host_rejects_invalid_modes():
    try:
        decide_host_policy(
            "https://adobe.wd5.myworkdayjobs.com/external/job/1",
            trusted_hosts={"adobe.wd5.myworkdayjobs.com": "Canary"},
        )
    except ValueError:
        return

    raise AssertionError("expected ValueError for invalid trusted host mode")


def test_known_low_yield_public_board_is_parked():
    decision = decide_host_policy("https://www.indeed.com/viewjob?jk=1")

    assert decision.mode == "supervised"
    assert decision.reason == "login_gate_prone"


def test_host_based_ats_classification_ignores_query_and_path_spoofing():
    query_spoof = decide_host_policy("https://evil.com/?next=https://boards.greenhouse.io/acme/jobs/1")
    evil_subdomain = decide_host_policy("https://jobs.ashbyhq.com.evil.test/acme/1")

    assert query_spoof.ats == "other"
    assert evil_subdomain.ats == "other"


def test_invalid_or_missing_host_fails_closed():
    decision = decide_host_policy("www.indeed.com/viewjob?jk=1")

    assert decision.mode == "supervised"
    assert decision.reason == "invalid_or_missing_host"
    assert decision.ats == "other"


def test_canary_hosts_allow_unattended_canary():
    lever = decide_host_policy("https://jobs.lever.co/acme/1")
    smartrecruiters = decide_host_policy("https://www.smartrecruiters.com/acme/job/1")

    assert lever.mode == "canary"
    assert lever.unattended_allowed is True
    assert smartrecruiters.mode == "canary"
    assert smartrecruiters.unattended_allowed is True


def test_supervised_workday_disallows_unattended():
    decision = decide_host_policy("https://adobe.wd5.myworkdayjobs.com/external/job/1")

    assert decision.mode == "supervised"
    assert decision.unattended_allowed is False


def test_host_policy_decision_label_combines_mode_and_reason():
    decision = HostPolicyDecision(mode="allow", reason="example_reason", host="example.com", ats="other")

    assert decision.label == "allow:example_reason"
