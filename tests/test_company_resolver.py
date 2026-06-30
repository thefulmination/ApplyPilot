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
):
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, company, location, site, application_url,
            audit_label, audit_score, fit_score, discovered_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-06-20T00:00:00+00:00')
        """,
        (url, title, company, location, site, application_url, audit_label, audit_score, fit_score),
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

