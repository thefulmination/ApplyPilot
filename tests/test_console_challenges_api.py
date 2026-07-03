"""Tests for GET /api/challenges: unions open auth_challenge rows with parked
(apply_status='challenge_pending') rows from BOTH apply_queue and linkedin_queue,
grouped kind -> host.

Uses the shared ``fleet_db`` fixture (disposable local Postgres, v3 schema applied).
build_challenges() opens its own pgqueue.connect(), so tests point it at the
disposable DSN via APPLYPILOT_FLEET_DSN (pgqueue.connect()'s env fallback).
"""
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import console_app


def test_build_challenges_unions_and_groups(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            # Open challenge with a matching parked apply_queue row.
            cur.execute(
                "INSERT INTO auth_challenge (url, kind, machine_owner, screenshot_url, raised_at) "
                "VALUES (%s, %s, %s, %s, now() - interval '2 hours')",
                ("https://boards.greenhouse.io/acme/jobs/1", "visible_captcha", "home", "https://shot/1.png"),
            )
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, apply_status, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, now() - interval '2 hours')",
                ("https://boards.greenhouse.io/acme/jobs/1", "Acme", "Engineer",
                 "https://boards.greenhouse.io/acme/jobs/1/apply", 8.5, "challenge_pending"),
            )
            # Parked linkedin_queue row with NO matching challenge row.
            cur.execute(
                "INSERT INTO linkedin_queue (url, company, title, application_url, score, apply_status, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, now() - interval '1 hour')",
                ("https://www.linkedin.com/jobs/view/2", "Beta", "Analyst",
                 "https://www.linkedin.com/jobs/view/2", 7.0, "challenge_pending"),
            )
            # Resolved challenge -- must NOT appear.
            cur.execute(
                "INSERT INTO auth_challenge (url, kind, machine_owner, screenshot_url, raised_at, resolved_at) "
                "VALUES (%s, %s, %s, %s, now() - interval '3 hours', now())",
                ("https://jobs.lever.co/other/3", "email_otp", "home", None),
            )
        conn.commit()

    result = console_app.build_challenges()

    assert result["counts"]["open_challenges"] == 1
    assert result["counts"]["parked"] == 2

    groups_by_key = {(g["kind"], g["host"]): g for g in result["groups"]}

    # The challenge+parked-apply_queue row groups under its real kind/host.
    captcha_group = groups_by_key[("visible_captcha", "boards.greenhouse.io")]
    assert len(captcha_group["rows"]) == 1
    row = captcha_group["rows"][0]
    assert row["url"] == "https://boards.greenhouse.io/acme/jobs/1"
    assert row["lane"] == "apply"
    assert row["company"] == "Acme"
    assert row["title"] == "Engineer"
    assert row["score"] == 8.5
    assert row["kind"] == "visible_captcha"
    assert row["machine"] == "home"
    assert row["screenshot_url"] == "https://shot/1.png"
    assert isinstance(row["age_hours"], float)
    assert row["age_hours"] >= 1.9  # ~2 hours old

    # The park-only linkedin_queue row groups under "(no challenge row)", www. stripped.
    no_challenge_group = groups_by_key[("(no challenge row)", "linkedin.com")]
    assert len(no_challenge_group["rows"]) == 1
    row2 = no_challenge_group["rows"][0]
    assert row2["url"] == "https://www.linkedin.com/jobs/view/2"
    assert row2["lane"] == "linkedin"
    assert row2["company"] == "Beta"
    assert row2["kind"] == "(no challenge row)"
    assert row2["machine"] is None
    assert row2["screenshot_url"] is None
    assert isinstance(row2["age_hours"], float)

    # The resolved challenge's url never appears anywhere.
    all_urls = {r["url"] for g in result["groups"] for r in g["rows"]}
    assert "https://jobs.lever.co/other/3" not in all_urls


def test_build_challenges_empty(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    result = console_app.build_challenges()
    assert result == {"groups": [], "counts": {"open_challenges": 0, "parked": 0}}


def test_challenges_route_returns_200_json(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "tok-challenges")

    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), console_app._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        # do_GET routes are read-only and NOT token-gated.
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/challenges", timeout=5) as resp:
            assert resp.status == 200
            import json
            body = json.loads(resp.read())
            assert body == {"groups": [], "counts": {"open_challenges": 0, "parked": 0}}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
