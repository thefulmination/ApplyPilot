"""Tests for the shared ``queue.challenge_summary`` helper and its consumers:
the CLI `challenges --grouped` flag on both home mains and (indirectly, via
build_challenges reusing the same host/kind derivation) the console's
/api/challenges detail view.

Uses the shared ``fleet_db`` fixture (disposable local Postgres, v3 schema applied).
"""
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import apply_home_main, linkedin_home_main, queue


def _seed(conn):
    with conn.cursor() as cur:
        # Open challenge + matching parked apply_queue row.
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
        # A second apply_queue parked row, SAME kind/host -- should bump the count to 2.
        cur.execute(
            "INSERT INTO auth_challenge (url, kind, machine_owner, screenshot_url, raised_at) "
            "VALUES (%s, %s, %s, %s, now() - interval '1 hour')",
            ("https://boards.greenhouse.io/acme/jobs/9", "visible_captcha", "home", None),
        )
        cur.execute(
            "INSERT INTO apply_queue (url, company, title, application_url, score, apply_status, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, now() - interval '1 hour')",
            ("https://boards.greenhouse.io/acme/jobs/9", "Acme", "Engineer2",
             "https://boards.greenhouse.io/acme/jobs/9/apply", 8.0, "challenge_pending"),
        )
        # Parked linkedin_queue row with NO matching challenge row.
        cur.execute(
            "INSERT INTO linkedin_queue (url, company, title, application_url, score, apply_status, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, now() - interval '1 hour')",
            ("https://www.linkedin.com/jobs/view/2", "Beta", "Analyst",
             "https://www.linkedin.com/jobs/view/2", 7.0, "challenge_pending"),
        )
        # Resolved challenge -- must NOT appear anywhere.
        cur.execute(
            "INSERT INTO auth_challenge (url, kind, machine_owner, screenshot_url, raised_at, resolved_at) "
            "VALUES (%s, %s, %s, %s, now() - interval '3 hours', now())",
            ("https://jobs.lever.co/other/3", "email_otp", "home", None),
        )
    conn.commit()


def test_challenge_summary_counts_per_kind_host(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn)
        rows = queue.challenge_summary(conn, None)

    by_key = {(r["kind"], r["host"]): r for r in rows}
    assert by_key[("visible_captcha", "boards.greenhouse.io")]["count"] == 2
    assert by_key[("(no challenge row)", "linkedin.com")]["count"] == 1
    # Resolved row's host never appears.
    assert ("email_otp", "jobs.lever.co") not in by_key


def test_challenge_summary_lane_apply_scoping(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn)
        rows = queue.challenge_summary(conn, "apply")

    by_key = {(r["kind"], r["host"]): r for r in rows}
    assert by_key[("visible_captcha", "boards.greenhouse.io")]["count"] == 2
    # The linkedin-only parked row must not appear when scoped to lane="apply".
    assert ("(no challenge row)", "linkedin.com") not in by_key


def test_challenge_summary_lane_linkedin_scoping(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn)
        rows = queue.challenge_summary(conn, "linkedin")

    by_key = {(r["kind"], r["host"]): r for r in rows}
    assert by_key[("(no challenge row)", "linkedin.com")]["count"] == 1
    # The apply-only parked rows must not appear when scoped to lane="linkedin".
    assert ("visible_captcha", "boards.greenhouse.io") not in by_key


def test_challenge_summary_empty(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        rows = queue.challenge_summary(conn, None)
    assert rows == []


def test_apply_home_challenges_grouped_prints_table(fleet_db, capsys):
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn)
    apply_home_main.main(["--dsn", fleet_db, "challenges", "--grouped"])
    out = capsys.readouterr().out
    assert "visible_captcha" in out
    assert "boards.greenhouse.io" in out
    assert "2" in out
    # LinkedIn-only group must not leak into the apply lane's grouped view.
    assert "linkedin.com" not in out


def test_linkedin_home_challenges_grouped_prints_table(fleet_db, capsys):
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn)
    linkedin_home_main.main(["--dsn", fleet_db, "challenges", "--grouped"])
    out = capsys.readouterr().out
    assert "linkedin.com" in out
    assert "(no challenge row)" in out
    # apply-only group must not leak into the linkedin lane's grouped view.
    assert "boards.greenhouse.io" not in out


def test_apply_home_challenges_without_grouped_unchanged(fleet_db, capsys):
    """No --grouped -> the existing per-row challenges output (list_challenges), unchanged."""
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn)
    apply_home_main.main(["--dsn", fleet_db, "challenges"])
    out = capsys.readouterr().out
    assert "raised_at" in out or "'kind': 'visible_captcha'" in out


def test_apply_home_challenges_does_not_leak_linkedin_auth_rows(fleet_db, capsys):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO linkedin_queue (url, application_url, score, status, apply_status) "
                "VALUES ('https://www.linkedin.com/jobs/view/99', "
                "'https://www.linkedin.com/jobs/view/99', 9, 'leased', 'challenge_pending')"
            )
            cur.execute(
                "INSERT INTO auth_challenge (url, kind, route, raised_at) VALUES "
                "('https://www.linkedin.com/jobs/view/99', 'login_gate', 'owner_inbox', "
                "now() - interval '2 days')"
            )
            conn.commit()

    apply_home_main.main(["--dsn", fleet_db, "challenges"])
    out = capsys.readouterr().out

    assert "https://www.linkedin.com/jobs/view/99" not in out


def test_apply_home_challenges_can_filter_stale_rows_and_show_park_state(fleet_db, capsys):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO auth_challenge (url, kind, route, raised_at) "
                "VALUES ('https://example.test/stale', 'login_gate', 'owner_inbox', "
                "now() - interval '3 days')"
            )
            cur.execute(
                "INSERT INTO apply_queue (url, application_url, score, status, apply_status, "
                "lane, lease_expires_at) VALUES ('https://example.test/stale', "
                "'https://example.test/stale', 9, 'leased', 'challenge_pending', 'ats', "
                "now() + interval '3650 days')"
            )
            conn.commit()

    apply_home_main.main(["--dsn", fleet_db, "challenges", "--stale-days", "1", "--limit", "1"])
    out = capsys.readouterr().out

    assert "https://example.test/stale" in out
    assert "age_hours" in out
    assert "parked" in out


def test_apply_home_challenges_can_filter_kind_and_prioritize_score(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, company, title, score, status, "
            "apply_status, lane, lease_owner, lease_expires_at) VALUES "
            "('priority-low', 'https://example.test/low', 'Low', 'Low', 7, 'leased', "
            "'challenge_pending', 'ats', 'w1', now() + interval '3650 days'), "
            "('priority-high', 'https://example.test/high', 'High', 'High', 9.8, 'leased', "
            "'challenge_pending', 'ats', 'w1', now() + interval '3650 days')"
        )
        cur.execute(
            "INSERT INTO auth_challenge (url, kind, route, raised_at) VALUES "
            "('priority-low', 'login_gate', 'owner_inbox', now() - interval '2 days'), "
            "('priority-high', 'login_gate', 'owner_inbox', now() - interval '1 day')"
        )
        conn.commit()

        rows = apply_home_main.list_challenges(conn, kind="login_gate", priority=True, limit=2)

    assert [row["url"] for row in rows] == ["priority-high", "priority-low"]


def test_apply_home_challenges_surfaces_parked_orphan_without_auth_row(fleet_db, capsys):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue (url, application_url, score, company, title, status, apply_status, "
                "lane, lease_owner, updated_at, lease_expires_at) VALUES "
                "('https://example.test/orphan', 'https://example.test/orphan', 9, 'Acme', "
                "'Chief of Staff', 'leased', 'challenge_pending', 'ats', 'worker-1', now() - interval '2 days', "
                "now() + interval '3650 days')"
            )
            conn.commit()

    apply_home_main.main(["--dsn", fleet_db, "challenges", "--stale-days", "1"])
    out = capsys.readouterr().out

    assert "https://example.test/orphan" in out
    assert "Chief of Staff" in out
    assert "Acme" in out
    assert "parked_without_open_challenge" in out
    assert "'orphan': True" in out


def test_apply_home_challenge_list_infers_missing_company_from_application_host(fleet_db):
    url = "https://indeed.com/viewjob?jk=company-fallback"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, company, title, score, status, apply_status, lane, lease_expires_at) "
            "VALUES (%s,'https://amazon.jobs/jobs/123',NULL,'Manager',8,'leased',"
            "'challenge_pending','ats',now()+interval '3650 days')",
            (url,),
        )
        cur.execute(
            "INSERT INTO auth_challenge (url, kind, route) VALUES (%s,'login_gate','owner_inbox')",
            (url,),
        )
        conn.commit()
        rows = apply_home_main.list_challenges(conn)

    assert rows[0]["company"] == "amazon.jobs"
    assert rows[0]["metadata_inferred_fields"] == ["company"]
    assert rows[0]["metadata_complete"] is True


def test_apply_home_challenges_handles_unicode_metadata(fleet_db, capsys):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO auth_challenge (url, kind, route, raised_at) "
                "VALUES ('https://example.test/unicode', 'login_gate', 'owner_inbox', "
                "now() - interval '3 days')"
            )
            cur.execute(
                "INSERT INTO apply_queue (url, application_url, company, title, score, status, "
                "apply_status, lane, lease_expires_at) VALUES "
                "('https://example.test/unicode', 'https://example.test/unicode', 'Mūn Labs', "
                "'Director, Strategy', 9, 'leased', 'challenge_pending', 'ats', now() + interval '3650 days')"
            )
            conn.commit()

    apply_home_main.main(["--dsn", fleet_db, "challenges", "--stale-days", "1"])
    out = capsys.readouterr().out

    assert "Mūn Labs" in out
