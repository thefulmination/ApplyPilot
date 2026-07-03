"""Tenant-aware auth-gated acquire filter (auth-gated-tenant-lane Task 3).

The home acquire path (launcher.acquire_job) has always parked auth-gated
rows (login/2FA walls the agent won't solve) as `auth_required` when
APPLYPILOT_SKIP_AUTH_GATED is on (the default) and inbox-auth is off. This
makes that skip TENANT-AWARE: a row whose host is a registered, non-halted,
under-cap tenant of the right status for this run's mode is allowed THROUGH
the gate instead of being parked. The fleet push (apply/fleet_sync.py) is
untouched by this change -- fleet workers must never receive auth-gated jobs
regardless of tenant status.
"""
from __future__ import annotations

import pytest

from applypilot import database, tenants
from applypilot.apply import launcher as L

AUTH_GATED_URL = "https://acme.myworkdayjobs.com/en-US/careers/job/12345"
NON_GATED_URL = "https://boards.greenhouse.io/acme/jobs/999"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(L, "get_connection", lambda: c)
    # Ensure a clean, deterministic auth-gated skip regardless of the ambient
    # environment (default is already skip-on, but be explicit + isolated).
    monkeypatch.setenv("APPLYPILOT_SKIP_AUTH_GATED", "1")
    monkeypatch.delenv("APPLYPILOT_INBOX_AUTH", raising=False)
    monkeypatch.delenv("APPLYPILOT_AUTH_GATED_MODE", raising=False)
    return c


def _ins(conn, url, *, title="Ops Role", company="Acme", audit=8.0):
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, "
        "tailored_resume_path, fit_score, audit_score) VALUES "
        "(?, ?, 'X', ?, ?, 'x', 8, ?)",
        (url, title, company, url, audit),
    )
    conn.commit()


def _job_status(conn, url):
    row = conn.execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    return row["apply_status"], row["apply_error"]


def test_auth_gated_parked_when_no_tenant_row(conn):
    _ins(conn, AUTH_GATED_URL)
    assert L.acquire_job(min_score=7) is None
    status, error = _job_status(conn, AUTH_GATED_URL)
    assert status == "auth_required"
    assert error == "auth_gate"


def test_auth_gated_allowed_when_supervised_and_mode_supervised(conn, monkeypatch):
    tenants.set_tenant(conn, "acme.myworkdayjobs.com", "supervised")
    monkeypatch.setenv("APPLYPILOT_AUTH_GATED_MODE", "supervised")
    _ins(conn, AUTH_GATED_URL)
    job = L.acquire_job(min_score=7)
    assert job is not None and job["url"] == AUTH_GATED_URL
    status, _ = _job_status(conn, AUTH_GATED_URL)
    assert status == "in_progress"


def test_auth_gated_not_allowed_when_supervised_but_mode_trusted(conn, monkeypatch):
    tenants.set_tenant(conn, "acme.myworkdayjobs.com", "supervised")
    monkeypatch.setenv("APPLYPILOT_AUTH_GATED_MODE", "trusted")
    _ins(conn, AUTH_GATED_URL)
    assert L.acquire_job(min_score=7) is None
    status, error = _job_status(conn, AUTH_GATED_URL)
    assert status == "auth_required"
    assert error == "auth_gate"


def test_auth_gated_not_allowed_when_supervised_and_mode_unset(conn):
    # Unset mode = trusted-only (safe default): supervised tenants never
    # apply unattended.
    tenants.set_tenant(conn, "acme.myworkdayjobs.com", "supervised")
    _ins(conn, AUTH_GATED_URL)
    assert L.acquire_job(min_score=7) is None
    status, error = _job_status(conn, AUTH_GATED_URL)
    assert status == "auth_required"
    assert error == "auth_gate"


def test_auth_gated_allowed_when_trusted_mode_unset(conn):
    tenants.set_tenant(conn, "acme.myworkdayjobs.com", "trusted", force=True)
    _ins(conn, AUTH_GATED_URL)
    job = L.acquire_job(min_score=7)
    assert job is not None and job["url"] == AUTH_GATED_URL


def test_auth_gated_parked_when_halted(conn):
    tenants.set_tenant(conn, "acme.myworkdayjobs.com", "trusted", force=True)
    tenants.halt_tenant(conn, "acme.myworkdayjobs.com", "2099-01-01T00:00:00+00:00")
    _ins(conn, AUTH_GATED_URL)
    assert L.acquire_job(min_score=7) is None
    status, error = _job_status(conn, AUTH_GATED_URL)
    assert status == "auth_required"
    assert error == "auth_gate"


def test_auth_gated_parked_when_at_daily_cap(conn):
    tenants.set_tenant(conn, "acme.myworkdayjobs.com", "trusted", force=True)
    conn.execute("UPDATE ats_tenants SET daily_cap = 0 WHERE host = ?",
                 ("acme.myworkdayjobs.com",))
    conn.commit()
    _ins(conn, AUTH_GATED_URL)
    assert L.acquire_job(min_score=7) is None
    status, error = _job_status(conn, AUTH_GATED_URL)
    assert status == "auth_required"
    assert error == "auth_gate"


def test_non_auth_gated_job_unaffected(conn, monkeypatch):
    # A normal (non-auth-gated) job must proceed to apply regardless of tenant
    # state or mode -- this filter only ever touches auth-gated rows.
    monkeypatch.setenv("APPLYPILOT_AUTH_GATED_MODE", "supervised")
    _ins(conn, NON_GATED_URL)
    job = L.acquire_job(min_score=7)
    assert job is not None and job["url"] == NON_GATED_URL


def test_tenant_scope_excludes_other_host(conn, monkeypatch):
    # --tenant scoping (APPLYPILOT_AUTH_GATED_TENANT_HOST) must actively EXCLUDE a
    # different-host job -- a regression to "set but ignored" would silently apply to
    # off-scope tenants. Two supervised tenants, scope pinned to one.
    OTHER_URL = "https://globex.wd1.myworkdayjobs.com/en-US/careers/job/777"
    tenants.set_tenant(conn, "acme.myworkdayjobs.com", "supervised")
    tenants.set_tenant(conn, "globex.wd1.myworkdayjobs.com", "supervised")
    monkeypatch.setenv("APPLYPILOT_AUTH_GATED_MODE", "supervised")
    monkeypatch.setenv("APPLYPILOT_AUTH_GATED_TENANT_HOST", "acme.myworkdayjobs.com")
    _ins(conn, AUTH_GATED_URL)   # acme -> in scope
    _ins(conn, OTHER_URL)        # globex -> out of scope

    first = L.acquire_job(min_score=7)
    assert first is not None and first["url"] == AUTH_GATED_URL   # only the in-scope host

    # Even as the sole remaining candidate, the out-of-scope host is never acquired.
    assert L.acquire_job(min_score=7) is None
    status, _ = _job_status(conn, OTHER_URL)
    assert status != "in_progress"   # globex left alone, not applied to
