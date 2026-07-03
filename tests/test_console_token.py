"""Tests for the console mutation token gate (Task 1): every POST /api/action is
gated on APPLYPILOT_CONSOLE_TOKEN, and GET / can arm a cookie via ?token=.
"""
from __future__ import annotations

import json
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from applypilot.fleet import console_app


# ---------------------------------------------------------------------------
# get_console_token
# ---------------------------------------------------------------------------
def test_get_console_token_honors_env_var(monkeypatch):
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "my-secret-token")
    assert console_app.get_console_token() == "my-secret-token"


def test_get_console_token_generates_and_caches_when_env_unset(monkeypatch):
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.delenv("APPLYPILOT_CONSOLE_TOKEN", raising=False)
    t1 = console_app.get_console_token()
    t2 = console_app.get_console_token()
    assert t1 == t2
    assert isinstance(t1, str) and len(t1) > 10


# ---------------------------------------------------------------------------
# _token_ok
# ---------------------------------------------------------------------------
def _stub_handler(headers: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(headers=headers)


def test_token_ok_accepts_header(monkeypatch):
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "tok-123")
    h = _stub_handler({"X-Console-Token": "tok-123"})
    assert console_app._token_ok(h) is True


def test_token_ok_rejects_wrong_header(monkeypatch):
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "tok-123")
    h = _stub_handler({"X-Console-Token": "wrong"})
    assert console_app._token_ok(h) is False


def test_token_ok_accepts_cookie(monkeypatch):
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "tok-123")
    h = _stub_handler({"Cookie": "console_token=tok-123"})
    assert console_app._token_ok(h) is True


def test_token_ok_rejects_wrong_cookie(monkeypatch):
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "tok-123")
    h = _stub_handler({"Cookie": "console_token=wrong"})
    assert console_app._token_ok(h) is False


def test_token_ok_rejects_missing_header_and_cookie(monkeypatch):
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "tok-123")
    h = _stub_handler({})
    assert console_app._token_ok(h) is False


# ---------------------------------------------------------------------------
# POST gating over a real ThreadingHTTPServer (mirrors test_outcome_dashboard.py's pattern)
# ---------------------------------------------------------------------------
@pytest.fixture()
def live_server(monkeypatch):
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "tok-live-123")
    calls = []

    def _fake_run_action(body):
        calls.append(body)
        return True, "ok"

    monkeypatch.setattr(console_app, "run_action", _fake_run_action)
    monkeypatch.setattr(console_app, "build_status", lambda: {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), console_app._Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", calls
    finally:
        server.shutdown()
        server.server_close()


def test_post_action_without_token_is_401_and_does_not_call_run_action(live_server):
    base, calls = live_server
    req = urllib.request.Request(
        f"{base}/api/action", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 401
    assert calls == []


def test_post_action_with_header_token_passes_through(live_server):
    base, calls = live_server
    req = urllib.request.Request(
        f"{base}/api/action", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "X-Console-Token": "tok-live-123"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    assert data["ok"] is True
    assert calls == [{}]


def test_post_action_with_wrong_header_token_is_401(live_server):
    base, calls = live_server
    req = urllib.request.Request(
        f"{base}/api/action", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "X-Console-Token": "nope"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 401
    assert calls == []


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def test_get_arm_url_sets_cookie(live_server):
    base, _ = live_server
    opener = urllib.request.build_opener(_NoRedirect())
    req = urllib.request.Request(f"{base}/?token=tok-live-123", method="GET")
    try:
        resp = opener.open(req)
        set_cookie = resp.headers.get("Set-Cookie")
        status = resp.status
    except urllib.error.HTTPError as e:
        set_cookie = e.headers.get("Set-Cookie")
        status = e.code
    assert status == 302
    assert set_cookie is not None
    assert "console_token=tok-live-123" in set_cookie
    assert "HttpOnly" in set_cookie


def test_get_arm_url_wrong_token_does_not_set_cookie(live_server):
    base, _ = live_server
    req = urllib.request.Request(f"{base}/?token=wrong-token", method="GET")
    with urllib.request.urlopen(req) as resp:
        set_cookie = resp.headers.get("Set-Cookie")
    assert set_cookie is None
