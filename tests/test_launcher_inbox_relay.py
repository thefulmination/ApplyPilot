"""Launcher relay mode returns the standard hint via the fleet, not local Gmail."""
from applypilot.apply import launcher
from applypilot.fleet import otp_relay


def test_relay_mode_returns_code_hint(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH", "1")
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH_MODE", "relay")
    monkeypatch.setenv("FLEET_PG_DSN", "postgresql://stub")

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def close(self): pass
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn: _Conn())
    monkeypatch.setattr(otp_relay, "request_code", lambda conn, **kw: 7)
    monkeypatch.setattr(otp_relay, "poll_for_code",
                        lambda conn, rid, **kw: otp_relay.RelayCode(value="246810", kind="code"))

    hint = launcher._poll_inbox_auth_hint({"url": "https://li/1",
                                           "application_url": "https://greenhouse.io/a"})
    assert hint is not None
    assert "code=246810" in hint and "fleet_relay" in hint


def test_relay_mode_magic_link_hint(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH", "1")
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH_MODE", "relay")
    monkeypatch.setenv("FLEET_PG_DSN", "postgresql://stub")

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def close(self): pass
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn: _Conn())
    monkeypatch.setattr(otp_relay, "request_code", lambda conn, **kw: 8)
    monkeypatch.setattr(otp_relay, "poll_for_code",
                        lambda conn, rid, **kw: otp_relay.RelayCode(
                            value="https://x/verify?t=abc", kind="magic_link"))

    hint = launcher._poll_inbox_auth_hint({"url": "j", "application_url": "https://greenhouse.io/a"})
    assert hint is not None and hint.startswith("magic_link=https://x/verify?t=abc")


def test_relay_mode_timeout_returns_none(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH", "1")
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH_MODE", "relay")
    monkeypatch.setenv("FLEET_PG_DSN", "postgresql://stub")

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def close(self): pass
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn: _Conn())
    monkeypatch.setattr(otp_relay, "request_code", lambda conn, **kw: 9)
    monkeypatch.setattr(otp_relay, "poll_for_code", lambda conn, rid, **kw: None)

    assert launcher._poll_inbox_auth_hint({"url": "j", "application_url": "https://greenhouse.io/a"}) is None


def test_prearm_only_uses_auth_gated_domains_not_generic_path_patterns(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH", "1")
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH_MODE", "relay")

    assert launcher._should_prearm_inbox_auth({
        "url": "https://indeed.com/viewjob?jk=1",
        "application_url": "https://fa-ewji-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/preview/83866",
    })
    assert not launcher._should_prearm_inbox_auth({
        "url": "https://remotejobs.org/remote-jobs/account-manager-readymode",
        "application_url": "https://remotejobs.org/remote-jobs/account-manager-readymode",
    })


def test_local_mode_unchanged_when_disabled(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_INBOX_AUTH", raising=False)  # disabled entirely
    assert launcher._poll_inbox_auth_hint({"url": "j", "application_url": "https://greenhouse.io/a"}) is None
