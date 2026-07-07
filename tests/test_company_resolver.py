from __future__ import annotations

from applypilot import company_resolver
from applypilot import database


def _insert_job(
    conn,
    *,
    url: str,
    title: str,
    company: str,
    location: str = "San Francisco, CA",
    site: str = "linkedin",
    application_url: str | None = None,
    audit_label: str | None = "recommended",
    audit_score: float | None = 8.5,
    fit_score: int | None = 8,
    linkedin_resolve_status: str | None = None,
    linkedin_unresolved_kind: str | None = None,
    linkedin_next_action: str | None = None,
):
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, company, location, site, application_url,
            audit_label, audit_score, fit_score, discovered_at,
            linkedin_resolve_status, linkedin_unresolved_kind, linkedin_next_action
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-06-20T00:00:00+00:00', ?, ?, ?)
        """,
        (
            url,
            title,
            company,
            location,
            site,
            application_url,
            audit_label,
            audit_score,
            fit_score,
            linkedin_resolve_status,
            linkedin_unresolved_kind,
            linkedin_next_action,
        ),
    )
    conn.commit()


def test_schema_adds_company_apply_url_resolution_columns(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")

    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}

    assert {
        "apply_url_resolved_at",
        "apply_url_resolution_strategy",
        "apply_url_resolution_confidence",
        "apply_url_resolution_source",
        "apply_url_resolution_error",
        "apply_url_resolution_attempts",
        "apply_url_resolution_matched_url",
    }.issubset(columns)


def test_resolves_unresolved_linkedin_job_from_existing_company_ats_match(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(company_resolver, "get_connection", lambda: conn)

    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/123",
        title="Chief of Staff",
        company="Acme, Inc.",
    )
    _insert_job(
        conn,
        url="https://boards.greenhouse.io/acme/jobs/456",
        title="Chief of Staff",
        company="Acme",
        site="greenhouse",
        application_url="https://boards.greenhouse.io/acme/jobs/456",
    )

    summary = company_resolver.run_resolver(company_resolver.CompanyResolverOptions(limit=10))

    row = conn.execute(
        """
        SELECT application_url, apply_url_resolution_strategy,
               apply_url_resolution_confidence, apply_url_resolution_matched_url
          FROM jobs
         WHERE url = 'https://www.linkedin.com/jobs/view/123'
        """
    ).fetchone()

    assert summary.counts == {"resolved_company_match": 1}
    assert row["application_url"] == "https://boards.greenhouse.io/acme/jobs/456"
    assert row["apply_url_resolution_strategy"] == "company_match"
    assert row["apply_url_resolution_confidence"] >= 0.9
    assert row["apply_url_resolution_matched_url"] == "https://boards.greenhouse.io/acme/jobs/456"


def test_skips_non_reconstruction_next_action_even_with_reconstruction_kind(
    tmp_path, monkeypatch
):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(company_resolver, "get_connection", lambda: conn)

    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/auth-required",
        title="Chief of Staff",
        company="Acme",
        audit_score=10.0,
        linkedin_resolve_status="unresolved",
        linkedin_unresolved_kind="apply_button_missing",
        linkedin_next_action="refresh_session",
    )
    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/reconstruct",
        title="Chief of Staff",
        company="Acme",
        audit_score=8.0,
        linkedin_resolve_status="unresolved",
        linkedin_unresolved_kind="apply_button_missing",
        linkedin_next_action="run_ats_reconstruction",
    )
    _insert_job(
        conn,
        url="https://boards.greenhouse.io/acme/jobs/456",
        title="Chief of Staff",
        company="Acme",
        site="greenhouse",
        application_url="https://boards.greenhouse.io/acme/jobs/456",
    )

    summary = company_resolver.run_resolver(company_resolver.CompanyResolverOptions(limit=10))

    rows = {
        row["url"]: row
        for row in conn.execute(
            """
            SELECT url, application_url, apply_url_resolution_strategy
              FROM jobs
             WHERE url LIKE 'https://www.linkedin.com/jobs/view/%'
            """
        ).fetchall()
    }

    assert summary.considered == 1
    assert summary.counts == {"resolved_company_match": 1}
    assert rows["https://www.linkedin.com/jobs/view/reconstruct"]["application_url"] == (
        "https://boards.greenhouse.io/acme/jobs/456"
    )
    assert rows["https://www.linkedin.com/jobs/view/auth-required"]["application_url"] is None
    assert rows["https://www.linkedin.com/jobs/view/auth-required"]["apply_url_resolution_strategy"] is None


def test_prioritizes_actionable_unresolved_rows_before_legacy_rows(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(company_resolver, "get_connection", lambda: conn)

    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/legacy",
        title="Chief of Staff",
        company="Acme",
        audit_score=10.0,
    )
    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/reconstruct",
        title="Chief of Staff",
        company="Acme",
        audit_score=8.0,
        linkedin_resolve_status="unresolved",
        linkedin_unresolved_kind="apply_button_missing",
        linkedin_next_action="run_ats_reconstruction",
    )
    _insert_job(
        conn,
        url="https://boards.greenhouse.io/acme/jobs/456",
        title="Chief of Staff",
        company="Acme",
        site="greenhouse",
        application_url="https://boards.greenhouse.io/acme/jobs/456",
    )

    summary = company_resolver.run_resolver(company_resolver.CompanyResolverOptions(limit=1))

    rows = {
        row["url"]: row
        for row in conn.execute(
            """
            SELECT url, application_url, apply_url_resolution_strategy
              FROM jobs
             WHERE url LIKE 'https://www.linkedin.com/jobs/view/%'
            """
        ).fetchall()
    }

    assert summary.considered == 1
    assert rows["https://www.linkedin.com/jobs/view/reconstruct"]["application_url"] == (
        "https://boards.greenhouse.io/acme/jobs/456"
    )
    assert rows["https://www.linkedin.com/jobs/view/legacy"]["application_url"] is None
    assert rows["https://www.linkedin.com/jobs/view/legacy"]["apply_url_resolution_strategy"] is None


def test_refresh_keeps_broad_unresolved_linkedin_fallback(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(company_resolver, "get_connection", lambda: conn)

    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/auth-required",
        title="Chief of Staff",
        company="Acme",
        linkedin_resolve_status="unresolved",
        linkedin_unresolved_kind="auth_required",
        linkedin_next_action="refresh_session",
    )
    _insert_job(
        conn,
        url="https://boards.greenhouse.io/acme/jobs/456",
        title="Chief of Staff",
        company="Acme",
        site="greenhouse",
        application_url="https://boards.greenhouse.io/acme/jobs/456",
    )

    summary = company_resolver.run_resolver(
        company_resolver.CompanyResolverOptions(limit=10, refresh=True)
    )

    row = conn.execute(
        """
        SELECT application_url, apply_url_resolution_strategy
          FROM jobs
         WHERE url = 'https://www.linkedin.com/jobs/view/auth-required'
        """
    ).fetchone()

    assert summary.counts == {"resolved_company_match": 1}
    assert row["application_url"] == "https://boards.greenhouse.io/acme/jobs/456"
    assert row["apply_url_resolution_strategy"] == "company_match"


def test_company_resolver_does_not_update_when_match_is_ambiguous(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(company_resolver, "get_connection", lambda: conn)

    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/ambiguous",
        title="Strategy and Operations Manager",
        company="Acme",
    )
    _insert_job(
        conn,
        url="https://jobs.lever.co/acme/one",
        title="Strategy and Operations Manager",
        company="Acme",
        site="lever",
        application_url="https://jobs.lever.co/acme/one",
    )
    _insert_job(
        conn,
        url="https://jobs.lever.co/acme/two",
        title="Strategy and Operations Manager",
        company="Acme",
        site="lever",
        application_url="https://jobs.lever.co/acme/two",
    )

    summary = company_resolver.run_resolver(company_resolver.CompanyResolverOptions(limit=10))

    row = conn.execute(
        """
        SELECT application_url, apply_url_resolution_strategy, apply_url_resolution_error
          FROM jobs
         WHERE url = 'https://www.linkedin.com/jobs/view/ambiguous'
        """
    ).fetchone()

    assert summary.counts == {"ambiguous": 1}
    assert row["application_url"] is None
    assert row["apply_url_resolution_strategy"] == "company_match"
    assert row["apply_url_resolution_error"] == "ambiguous_company_match"
