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
    assert result["summary"]["fresh_rows"] == 2
    assert result["summary"]["stale_rows"] == 0
    assert result["summary"]["missing_metadata_rows"] == 0
    assert result["summary"]["by_kind"] == {"(no challenge row)": 1, "visible_captcha": 1}
    assert result["triage"] == {
        "clear_first": [],
        "resolve_first": [
            {
                "host": "linkedin.com",
                "kind": "(no challenge row)",
                "rows": 1,
                "stale_rows": 0,
                "fresh_rows": 1,
                "missing_metadata_rows": 0,
                "park_only_rows": 1,
                "oldest_age_hours": pytest.approx(1.0, rel=0.1),
                "lane": "linkedin",
                "machines": [],
            },
            {
                "host": "boards.greenhouse.io",
                "kind": "visible_captcha",
                "rows": 1,
                "stale_rows": 0,
                "fresh_rows": 1,
                "missing_metadata_rows": 0,
                "park_only_rows": 0,
                "oldest_age_hours": pytest.approx(2.0, rel=0.1),
                "lane": "apply",
                "machines": ["home"],
            },
        ],
        "stale_by_host": {},
        "missing_metadata_by_host": {},
    }

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
    assert row["staleness"] == "fresh"
    assert row["has_metadata"] is True

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
    assert row2["staleness"] == "fresh"
    assert row2["has_metadata"] is True

    # The resolved challenge's url never appears anywhere.
    all_urls = {r["url"] for g in result["groups"] for r in g["rows"]}
    assert "https://jobs.lever.co/other/3" not in all_urls


def test_build_challenges_empty(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    result = console_app.build_challenges()
    assert result == {
        "groups": [],
        "counts": {"open_challenges": 0, "parked": 0},
        "summary": {
            "rows": 0,
            "fresh_rows": 0,
            "stale_rows": 0,
            "missing_metadata_rows": 0,
            "by_kind": {},
            "by_staleness": {},
        },
        "triage": {
            "clear_first": [],
            "resolve_first": [],
            "stale_by_host": {},
            "missing_metadata_by_host": {},
        },
    }


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
            assert body == {
                "groups": [],
                "counts": {"open_challenges": 0, "parked": 0},
                "summary": {
                    "rows": 0,
                    "fresh_rows": 0,
                    "stale_rows": 0,
                    "missing_metadata_rows": 0,
                    "by_kind": {},
                    "by_staleness": {},
                },
                "triage": {
                    "clear_first": [],
                    "resolve_first": [],
                    "stale_by_host": {},
                    "missing_metadata_by_host": {},
                },
            }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_build_challenges_humanizes_machine_name(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO auth_challenge (url, kind, machine_owner, screenshot_url, raised_at) "
                "VALUES (%s, %s, %s, %s, now() - interval '1 hour')",
                ("https://www.indeed.com/viewjob?jk=abc", "captcha", "m4", "https://shot/4.png"),
            )
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, apply_status, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, now() - interval '1 hour')",
                ("https://www.indeed.com/viewjob?jk=abc", "Gamma", "Builder", "https://www.indeed.com/viewjob?jk=abc/apply", 6.2, "challenge_pending"),
            )
        conn.commit()

    result = console_app.build_challenges()
    rows = [r for group in result["groups"] for r in group["rows"] if r["kind"] == "captcha"]
    assert len(rows) == 1
    assert rows[0]["machine"] == "gggtower"


def test_build_challenges_infers_company_display_from_application_host(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    url = "https://www.indeed.com/viewjob?jk=inferred-company"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, company, title, score, status, apply_status, lane, lease_expires_at) "
            "VALUES (%s,'https://www.amazon.jobs/jobs/123',NULL,'Business Development Manager',8,"
            "'leased','challenge_pending','ats',now()+interval '3650 days')",
            (url,),
        )
        cur.execute(
            "INSERT INTO auth_challenge (url, kind, route) VALUES (%s,'login_gate','owner_inbox')",
            (url,),
        )
        conn.commit()

    result = console_app.build_challenges()
    row = next(row for group in result["groups"] for row in group["rows"] if row["url"] == url)
    assert row["company"] == "amazon.jobs"
    assert row["title"] == "Business Development Manager"
    assert row["metadata_inferred_fields"] == ["company"]
    assert row["has_metadata"] is True
    assert result["summary"]["missing_metadata_rows"] == 0


def test_build_challenges_marks_stale_rows_and_missing_metadata(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO auth_challenge (url, kind, machine_owner, raised_at) "
                "VALUES (%s, %s, %s, now() - interval '30 hours')",
                ("https://remotejobs.org/old", "login_gate", "m2"),
            )
            cur.execute(
                "INSERT INTO linkedin_queue (url, application_url, score, apply_status, updated_at) "
                "VALUES (%s, %s, %s, %s, now() - interval '40 hours')",
                ("https://www.linkedin.com/jobs/view/old-parked",
                 "https://www.linkedin.com/jobs/view/old-parked",
                 0.0,
                 "challenge_pending"),
            )
        conn.commit()

    result = console_app.build_challenges()
    groups = {(g["kind"], g["host"]): g for g in result["groups"]}
    old_login = groups[("login_gate", "remotejobs.org")]["rows"][0]
    old_parked = groups[("(no challenge row)", "linkedin.com")]["rows"][0]
    assert old_login["staleness"] == "stale"
    assert old_parked["staleness"] == "stale"
    assert old_parked["has_metadata"] is False
    assert result["summary"]["stale_rows"] == 2
    assert result["summary"]["missing_metadata_rows"] == 2
    assert result["summary"]["by_staleness"] == {"stale": 2}
    assert result["triage"]["stale_by_host"] == {
        "linkedin.com": 1,
        "remotejobs.org": 1,
    }
    assert result["triage"]["missing_metadata_by_host"] == {
        "linkedin.com": 1,
        "remotejobs.org": 1,
    }
    assert result["triage"]["clear_first"] == [
        {
            "host": "linkedin.com",
            "kind": "(no challenge row)",
            "rows": 1,
            "stale_rows": 1,
            "fresh_rows": 0,
            "missing_metadata_rows": 1,
            "park_only_rows": 1,
            "oldest_age_hours": pytest.approx(40.0, rel=0.1),
            "lane": "linkedin",
            "machines": [],
        },
        {
            "host": "remotejobs.org",
            "kind": "login_gate",
            "rows": 1,
            "stale_rows": 1,
            "fresh_rows": 0,
            "missing_metadata_rows": 1,
            "park_only_rows": 0,
            "oldest_age_hours": pytest.approx(30.0, rel=0.1),
            "lane": None,
            "machines": ["tarpon"],
        },
    ]
    assert result["triage"]["resolve_first"] == []


def test_build_challenges_triage_prioritizes_clear_first_groups(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO auth_challenge (url, kind, machine_owner, raised_at) "
                "VALUES (%s, %s, %s, now() - interval '50 hours'), "
                "(%s, %s, %s, now() - interval '28 hours'), "
                "(%s, %s, %s, now() - interval '2 hours')",
                (
                    "https://example.com/jobs/old-1", "login_gate", "m2",
                    "https://example.com/jobs/old-2", "login_gate", "m4",
                    "https://example.com/jobs/fresh-1", "visible_captcha", "m4",
                ),
            )
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, apply_status, updated_at) "
                "VALUES "
                "(%s, %s, %s, %s, %s, %s, now() - interval '50 hours'), "
                "(%s, %s, %s, %s, %s, %s, now() - interval '28 hours'), "
                "(%s, %s, %s, %s, %s, %s, now() - interval '2 hours')",
                (
                    "https://example.com/jobs/old-1", "Acme", "Old 1", "https://example.com/jobs/old-1/apply", 8.5, "challenge_pending",
                    "https://example.com/jobs/old-2", None, None, "https://example.com/jobs/old-2/apply", 6.1, "challenge_pending",
                    "https://example.com/jobs/fresh-1", "Acme", "Fresh 1", "https://example.com/jobs/fresh-1/apply", 9.2, "challenge_pending",
                ),
            )
        conn.commit()

    result = console_app.build_challenges()
    assert result["triage"]["clear_first"] == [
        {
            "host": "example.com",
            "kind": "login_gate",
            "rows": 2,
            "stale_rows": 2,
            "fresh_rows": 0,
            "missing_metadata_rows": 1,
            "park_only_rows": 0,
            "oldest_age_hours": pytest.approx(50.0, rel=0.1),
            "lane": "apply",
            "machines": ["gggtower", "tarpon"],
        },
    ]
    assert result["triage"]["resolve_first"] == [
        {
            "host": "example.com",
            "kind": "visible_captcha",
            "rows": 1,
            "stale_rows": 0,
            "fresh_rows": 1,
            "missing_metadata_rows": 0,
            "park_only_rows": 0,
            "oldest_age_hours": pytest.approx(2.0, rel=0.1),
            "lane": "apply",
            "machines": ["gggtower"],
        },
    ]
