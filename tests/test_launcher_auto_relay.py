"""A credential-less remote worker (fleet DSN, no local Gmail) auto-selects the OTP
relay for email verification, so an offsite worker needs zero local config. Explicit
env vars still win; the home box (has local Gmail) is unchanged."""
from applypilot.apply import launcher


def test_auto_relay_when_fleet_dsn_and_no_local_gmail(monkeypatch):
    monkeypatch.setenv("FLEET_PG_DSN", "host=x")
    monkeypatch.delenv("APPLYPILOT_INBOX_AUTH", raising=False)
    monkeypatch.delenv("APPLYPILOT_INBOX_AUTH_MODE", raising=False)
    monkeypatch.setattr(launcher, "_local_gmail_available", lambda: False)
    assert launcher._auto_relay() is True
    assert launcher._inbox_auth_enabled() is True
    assert launcher._inbox_auth_mode() == "relay"


def test_home_box_with_local_gmail_stays_local(monkeypatch):
    monkeypatch.setenv("FLEET_PG_DSN", "host=x")
    monkeypatch.delenv("APPLYPILOT_INBOX_AUTH", raising=False)
    monkeypatch.delenv("APPLYPILOT_INBOX_AUTH_MODE", raising=False)
    monkeypatch.setattr(launcher, "_local_gmail_available", lambda: True)
    assert launcher._auto_relay() is False
    assert launcher._inbox_auth_enabled() is False  # not auto-enabled; needs the explicit flag
    assert launcher._inbox_auth_mode() == "local"


def test_no_fleet_dsn_means_no_auto_relay(monkeypatch):
    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    monkeypatch.delenv("APPLYPILOT_INBOX_AUTH", raising=False)
    monkeypatch.delenv("APPLYPILOT_INBOX_AUTH_MODE", raising=False)
    monkeypatch.setattr(launcher, "_local_gmail_available", lambda: False)
    assert launcher._auto_relay() is False
    assert launcher._inbox_auth_enabled() is False
    assert launcher._inbox_auth_mode() == "local"


def test_explicit_env_wins_over_auto(monkeypatch):
    monkeypatch.setenv("FLEET_PG_DSN", "host=x")
    monkeypatch.setattr(launcher, "_local_gmail_available", lambda: False)
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH_MODE", "local")
    assert launcher._inbox_auth_mode() == "local"
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH_MODE", "relay")
    assert launcher._inbox_auth_mode() == "relay"
