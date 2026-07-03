"""Tests for the console Challenges page section (Task 4): the embedded HTML fetches
GET /api/challenges, renders groups, and wires buttons to POST /api/action. Smoke-level
only — reuses the live-server pattern from test_console_token.py / test_console_challenges_api.py.
"""
from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from applypilot.fleet import console_app


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@pytest.fixture()
def live_server(monkeypatch):
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "tok-page-123")

    server = ThreadingHTTPServer(("127.0.0.1", 0), console_app._Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_index_page_has_challenges_section_and_fetch(live_server):
    with urllib.request.urlopen(f"{live_server}/") as resp:
        assert resp.status == 200
        html = resp.read().decode("utf-8")
    assert 'id="challenges"' in html
    assert "/api/challenges" in html


def test_index_page_wires_action_credentials_same_origin(live_server):
    with urllib.request.urlopen(f"{live_server}/") as resp:
        html = resp.read().decode("utf-8")
    # The action POST helper must send the token cookie (credentials: 'same-origin').
    assert "credentials: \"same-origin\"" in html or "credentials:\"same-origin\"" in html
    assert "/api/action" in html


def test_index_page_has_token_expired_banner_text(live_server):
    with urllib.request.urlopen(f"{live_server}/") as resp:
        html = resp.read().decode("utf-8")
    assert "token expired" in html
    assert "?token=" in html


def test_get_token_url_still_302_and_sets_cookie(live_server):
    """Regression: Task 1's ?token= arming must still work after the Task 4 HTML edits."""
    opener = urllib.request.build_opener(_NoRedirect())
    req = urllib.request.Request(f"{live_server}/?token=tok-page-123", method="GET")
    try:
        resp = opener.open(req)
        set_cookie = resp.headers.get("Set-Cookie")
        status = resp.status
    except urllib.error.HTTPError as e:
        set_cookie = e.headers.get("Set-Cookie")
        status = e.code
    assert status == 302
    assert set_cookie is not None
    assert "console_token=tok-page-123" in set_cookie
    assert "HttpOnly" in set_cookie


def test_get_wrong_token_no_cookie(live_server):
    req = urllib.request.Request(f"{live_server}/?token=wrong", method="GET")
    with urllib.request.urlopen(req) as resp:
        set_cookie = resp.headers.get("Set-Cookie")
        assert resp.status == 200
    assert set_cookie is None


# ---------------------------------------------------------------------------
# Rendered challenge rows (needs PG — fleet_db fixture, seeded like Task 2's test).
# Only the /api/challenges payload shape is asserted here; the JS rendering itself
# is exercised indirectly via the smoke tests above (no headless browser in this repo).
# ---------------------------------------------------------------------------
def test_challenges_api_payload_has_fields_the_page_js_expects(fleet_db, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from applypilot.apply import pgqueue

    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO auth_challenge (url, kind, machine_owner, screenshot_url, raised_at) "
                "VALUES (%s, %s, %s, %s, now() - interval '1 hour')",
                ("https://boards.greenhouse.io/acme/jobs/1", "visible_captcha", "home",
                 "https://shot/1.png"),
            )
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, "
                "apply_status, updated_at) VALUES (%s, %s, %s, %s, %s, %s, now() - interval '1 hour')",
                ("https://boards.greenhouse.io/acme/jobs/1", "Acme", "Engineer",
                 "https://boards.greenhouse.io/acme/jobs/1/apply", 8.5, "challenge_pending"),
            )
        conn.commit()

    result = console_app.build_challenges()
    assert result["groups"], "expected at least one group"
    group = result["groups"][0]
    assert set(group.keys()) == {"kind", "host", "rows"}
    row = group["rows"][0]
    for field in ("url", "lane", "company", "title", "score", "kind", "machine",
                  "screenshot_url", "age_hours"):
        assert field in row
    assert row["lane"] == "apply"  # verbatim lane value the page's fetch buttons must send
