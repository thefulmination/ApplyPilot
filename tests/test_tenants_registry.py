"""Tests for the ats_tenants registry (Task 1 of auth-gated-tenant-lane)."""

import pytest

from applypilot import database, tenants


def _setup(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    return conn


def test_unknown_host_returns_excluded(tmp_path):
    conn = _setup(tmp_path)
    assert tenants.tenant_status(conn, "myworkday.wd1.myworkdayjobs.com") == "excluded"


def test_set_and_list_round_trip(tmp_path):
    conn = _setup(tmp_path)
    row = tenants.set_tenant(conn, "acme.wd1.myworkdayjobs.com", "supervised")
    assert row["host"] == "acme.wd1.myworkdayjobs.com"
    assert row["status"] == "supervised"

    assert tenants.tenant_status(conn, "acme.wd1.myworkdayjobs.com") == "supervised"

    rows = tenants.list_tenants(conn)
    assert len(rows) == 1
    assert rows[0]["host"] == "acme.wd1.myworkdayjobs.com"
    assert rows[0]["status"] == "supervised"
    assert rows[0]["clean_submits"] == 0
    assert rows[0]["failed_submits"] == 0
    assert rows[0]["daily_cap"] == 5


def test_set_tenant_bogus_status_raises(tmp_path):
    conn = _setup(tmp_path)
    with pytest.raises(ValueError):
        tenants.set_tenant(conn, "acme.wd1.myworkdayjobs.com", "bogus")


def test_promote_to_trusted_without_evidence_raises(tmp_path):
    conn = _setup(tmp_path)
    tenants.set_tenant(conn, "acme.wd1.myworkdayjobs.com", "supervised")
    with pytest.raises(ValueError):
        tenants.set_tenant(conn, "acme.wd1.myworkdayjobs.com", "trusted")


def test_promote_to_trusted_with_force_succeeds(tmp_path):
    conn = _setup(tmp_path)
    tenants.set_tenant(conn, "acme.wd1.myworkdayjobs.com", "supervised")
    row = tenants.set_tenant(conn, "acme.wd1.myworkdayjobs.com", "trusted", force=True)
    assert row["status"] == "trusted"


def test_promote_to_trusted_with_sufficient_evidence_succeeds(tmp_path):
    conn = _setup(tmp_path)
    tenants.set_tenant(conn, "acme.wd1.myworkdayjobs.com", "supervised")
    for _ in range(3):
        tenants.record_submit(conn, "acme.wd1.myworkdayjobs.com", ok=True, result="submitted")
    row = tenants.set_tenant(conn, "acme.wd1.myworkdayjobs.com", "trusted")
    assert row["status"] == "trusted"


def test_record_submit_increments_correct_counters(tmp_path):
    conn = _setup(tmp_path)
    host = "acme.wd1.myworkdayjobs.com"
    tenants.set_tenant(conn, host, "supervised")

    tenants.record_submit(conn, host, ok=True, result="submitted")
    row = tenants.tenant_status  # sanity: still a function
    assert callable(row)

    rows = tenants.list_tenants(conn)
    assert rows[0]["clean_submits"] == 1
    assert rows[0]["failed_submits"] == 0
    assert rows[0]["last_result"] == "submitted"

    tenants.record_submit(conn, host, ok=False, result="captcha_blocked")
    rows = tenants.list_tenants(conn)
    assert rows[0]["clean_submits"] == 1
    assert rows[0]["failed_submits"] == 1
    assert rows[0]["last_result"] == "captcha_blocked"


def test_record_submit_creates_row_if_absent(tmp_path):
    conn = _setup(tmp_path)
    host = "newhost.wd5.myworkdayjobs.com"
    tenants.record_submit(conn, host, ok=True, result="submitted")
    assert tenants.tenant_status(conn, host) == "excluded"
    rows = tenants.list_tenants(conn)
    assert rows[0]["clean_submits"] == 1


def test_halt_and_is_halted_honor_timestamp(tmp_path):
    conn = _setup(tmp_path)
    host = "acme.wd1.myworkdayjobs.com"
    tenants.set_tenant(conn, host, "supervised")

    tenants.halt_tenant(conn, host, "2026-08-01T00:00:00+00:00")

    assert tenants.is_halted(conn, host, "2026-07-15T00:00:00+00:00") is True
    assert tenants.is_halted(conn, host, "2026-09-01T00:00:00+00:00") is False


def test_is_halted_false_when_never_halted(tmp_path):
    conn = _setup(tmp_path)
    host = "acme.wd1.myworkdayjobs.com"
    tenants.set_tenant(conn, host, "supervised")
    assert tenants.is_halted(conn, host, "2026-07-15T00:00:00+00:00") is False


def test_tenant_status_table_absent_returns_excluded():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert tenants.tenant_status(conn, "acme.wd1.myworkdayjobs.com") == "excluded"


def test_host_of_strips_www():
    assert tenants._host_of("https://www.acme.wd1.myworkdayjobs.com/en-US/careers/job/1") == \
        "acme.wd1.myworkdayjobs.com"
    assert tenants._host_of("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/1") == \
        "acme.wd1.myworkdayjobs.com"


def test_submits_today_counts_only_today(tmp_path, monkeypatch):
    conn = _setup(tmp_path)
    host = "acme.wd1.myworkdayjobs.com"

    def _insert_application(job_url, applied_at):
        now = "2026-07-03T00:00:00+00:00"
        conn.execute(
            "INSERT INTO applications (job_url, status, applied_at, created_at, updated_at) "
            "VALUES (?, 'applied', ?, ?, ?)",
            (job_url, applied_at, now, now),
        )

    _insert_application(f"https://{host}/job/1", "2026-07-03T10:00:00+00:00")
    _insert_application(f"https://{host}/job/2", "2026-07-03T18:00:00+00:00")
    _insert_application(f"https://{host}/job/3", "2026-07-01T10:00:00+00:00")
    _insert_application("https://otherhost.wd1.myworkdayjobs.com/job/4", "2026-07-03T10:00:00+00:00")
    conn.commit()

    assert tenants.submits_today(conn, host, today_iso="2026-07-03") == 2
