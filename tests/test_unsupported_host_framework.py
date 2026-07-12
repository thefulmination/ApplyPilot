from applypilot.fleet.host_framework import adapter_supported, unregistered_host_policy


def test_unsupported_host_routes_to_exception_before_paid_execution(monkeypatch):
    assert unregistered_host_policy("careers.example.com") == {
        "session_required": False,
        "tenant_profile_id": None,
        "routing_required": True,
        "execution_route": "exception",
        "host_policy": "adapter_unsupported",
    }


def test_greenhouse_requires_enabled_adapter(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_GREENHOUSE_ADAPTER", raising=False)
    assert adapter_supported("boards.greenhouse.io") is False
    monkeypatch.setenv("APPLYPILOT_GREENHOUSE_ADAPTER", "1")
    assert unregistered_host_policy("boards.greenhouse.io")["execution_route"] == "deterministic"


def test_workday_never_bypasses_tenant_registry(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_WORKDAY_ADAPTER_ENABLED", "1")
    assert adapter_supported("acme.wd5.myworkdayjobs.com") is False
