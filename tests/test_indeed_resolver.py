import pytest

from applypilot import database
from applypilot import indeed_resolver


def test_classify_snapshot_detects_company_site_apply():
    control = indeed_resolver.ApplyControl(
        text="Apply on company site",
        href=None,
        selector="#apply",
    )
    snapshot = indeed_resolver.PageSnapshot(
        url="https://www.indeed.com/viewjob?jk=abc",
        body_text="Apply on company site",
        controls=(control,),
    )

    decision = indeed_resolver.classify_snapshot(snapshot)

    assert decision.status == "needs_click"
    assert decision.control == control


def test_classify_snapshot_detects_hosted_indeed_apply():
    control = indeed_resolver.ApplyControl(
        text="Apply now",
        href="https://smartapply.indeed.com/beta/indeedapply/form/contact-info",
        selector="#indeed-apply",
    )
    snapshot = indeed_resolver.PageSnapshot(
        url="https://www.indeed.com/viewjob?jk=abc",
        body_text="Apply now",
        controls=(control,),
    )

    decision = indeed_resolver.classify_snapshot(snapshot)

    assert decision.status == "hosted_apply"
    assert decision.control == control
    assert decision.final_url is None


def test_classify_snapshot_detects_direct_external_href():
    control = indeed_resolver.ApplyControl(
        text="Apply on company site",
        href="https://jobs.ashbyhq.com/acme/abc",
        selector="#company-apply",
    )
    snapshot = indeed_resolver.PageSnapshot(
        url="https://www.indeed.com/viewjob?jk=abc",
        body_text="Apply on company site",
        controls=(control,),
    )

    decision = indeed_resolver.classify_snapshot(snapshot)

    assert decision.status == "resolved_offsite"
    assert decision.final_url == "https://jobs.ashbyhq.com/acme/abc"
    assert decision.control == control


def test_classify_snapshot_missing_button_is_actionable_unresolved():
    snapshot = indeed_resolver.PageSnapshot(
        url="https://www.indeed.com/viewjob?jk=abc",
        body_text="Senior operator role",
        controls=(),
    )

    decision = indeed_resolver.classify_snapshot(snapshot)

    assert decision.status == "unresolved"
    assert decision.unresolved_kind == "apply_button_missing"
    assert decision.next_action == "run_ats_reconstruction"
    assert decision.error == "no_primary_apply_button"


def test_classify_snapshot_detects_checkpoint_or_captcha():
    snapshot = indeed_resolver.PageSnapshot(
        url="https://www.indeed.com/viewjob?jk=abc",
        body_text="Verify you are human to continue",
        controls=(),
    )

    decision = indeed_resolver.classify_snapshot(snapshot)

    assert decision.status == "unresolved"
    assert decision.unresolved_kind == "checkpoint_or_captcha"
    assert decision.next_action == "pause_resolver"
    assert decision.error == "indeed_checkpoint"


def test_classify_snapshot_detects_unavailable_job():
    snapshot = indeed_resolver.PageSnapshot(
        url="https://www.indeed.com/viewjob?jk=abc",
        body_text="This job has expired on Indeed",
        controls=(),
    )

    decision = indeed_resolver.classify_snapshot(snapshot)

    assert decision.status == "unavailable"
    assert decision.error == "indeed_unavailable"


class _FakePopupInfo:
    def __init__(self, popup):
        self.value = popup

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakePopup:
    def __init__(self, url):
        self.url = url
        self.closed = False

    def wait_for_load_state(self, *_args, **_kwargs):
        return None

    def close(self):
        self.closed = True


class _FakeLocator:
    def __init__(self, page):
        self._page = page

    @property
    def first(self):
        return self

    def click(self, **_kwargs):
        if self._page.click_error is not None:
            raise self._page.click_error
        self._page.clicked = True


class _FakePage:
    def __init__(self, *, popup_url=None, popup_error=None, click_error=None):
        self.popup_url = popup_url
        self.popup_error = popup_error
        self.click_error = click_error
        self.clicked = False
        self.popup = None

    def expect_popup(self, **_kwargs):
        if self.popup_error is not None:
            raise self.popup_error
        self.popup = _FakePopup(self.popup_url)
        return _FakePopupInfo(self.popup)

    def get_by_text(self, *_args, **_kwargs):
        return _FakeLocator(self)


def test_click_and_capture_external_popup_resolves_offsite():
    page = _FakePage(popup_url="https://jobs.lever.co/acme/abc")
    control = indeed_resolver.ApplyControl("Apply on company site", None, "#apply")

    decision = indeed_resolver._click_and_capture_external(page, control, timeout_ms=1)

    assert decision.status == "resolved_offsite"
    assert decision.final_url == "https://jobs.lever.co/acme/abc"
    assert decision.control == control
    assert page.clicked is True
    assert page.popup is not None
    assert page.popup.closed is True


def test_click_and_capture_no_popup_is_unresolved():
    page = _FakePage(popup_error=TimeoutError("no popup"))
    control = indeed_resolver.ApplyControl("Apply on company site", None, "#apply")

    decision = indeed_resolver._click_and_capture_external(page, control, timeout_ms=1)

    assert decision.status == "unresolved"
    assert decision.unresolved_kind == "outbound_not_observed"
    assert decision.next_action == "retry_with_network_capture"
    assert "no popup" in (decision.error or "")


def test_click_and_capture_source_platform_popup_is_unresolved():
    page = _FakePage(popup_url="https://www.indeed.com/applystart?jk=abc")
    control = indeed_resolver.ApplyControl("Apply on company site", None, "#apply")

    decision = indeed_resolver._click_and_capture_external(page, control, timeout_ms=1)

    assert decision.status == "unresolved"
    assert decision.unresolved_kind == "outbound_still_source_platform"
    assert decision.next_action == "run_url_unwrapper"
    assert decision.error == "https://www.indeed.com/applystart?jk=abc"
    assert page.popup is not None
    assert page.popup.closed is True


def test_click_and_capture_known_source_platform_popup_is_unresolved():
    page = _FakePage(popup_url="https://www.linkedin.com/jobs/view/123")
    control = indeed_resolver.ApplyControl("Apply on company site", None, "#apply")

    decision = indeed_resolver._click_and_capture_external(page, control, timeout_ms=1)

    assert decision.status == "unresolved"
    assert decision.unresolved_kind == "outbound_still_source_platform"
    assert decision.next_action == "run_url_unwrapper"
    assert decision.error == "https://www.linkedin.com/jobs/view/123"
    assert page.popup is not None
    assert page.popup.closed is True


def _insert_job(
    conn,
    *,
    url: str,
    site: str = "indeed",
    application_url: str | None = None,
    audit_label: str | None = "recommended",
    audit_score: float | None = 8.5,
    fit_score: int | None = 8,
    duplicate_of_url: str | None = None,
    liveness_status: str | None = None,
    applied_at: str | None = None,
    strategy: str | None = None,
):
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, site, company, application_url, audit_label, audit_score,
            fit_score, duplicate_of_url, liveness_status, applied_at,
            apply_url_resolution_strategy, discovered_at
        )
        VALUES (?, 'Chief of Staff', ?, 'Acme', ?, ?, ?, ?, ?, ?, ?, ?, '2026-06-20T00:00:00+00:00')
        """,
        (
            url,
            site,
            application_url,
            audit_label,
            audit_score,
            fit_score,
            duplicate_of_url,
            liveness_status,
            applied_at,
            strategy,
        ),
    )
    conn.commit()


def test_fetch_candidates_selects_unresolved_indeed_rows(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(indeed_resolver, "get_connection", lambda: conn)

    _insert_job(conn, url="https://www.indeed.com/viewjob?jk=low", audit_label="low", audit_score=9.9)
    _insert_job(conn, url="https://www.indeed.com/viewjob?jk=dupe", duplicate_of_url="https://x")
    _insert_job(conn, url="https://www.indeed.com/viewjob?jk=dead", liveness_status="dead")
    _insert_job(conn, url="https://www.indeed.com/viewjob?jk=applied", applied_at="2026-06-20T01:00:00+00:00")
    _insert_job(conn, url="https://www.indeed.com/viewjob?jk=resolved", strategy="indeed_deterministic")
    _insert_job(conn, url="https://www.indeed.com/viewjob?jk=company-match", strategy="company_match")
    _insert_job(conn, url="https://company.example/jobs/1", site="greenhouse")
    _insert_job(conn, url="https://notindeed.com/job", site="greenhouse")
    _insert_job(conn, url="https://company.example/jobs/indeed.com-shadow", site="greenhouse")
    _insert_job(conn, url="https://www.indeed.com/viewjob?jk=priority", audit_label="priority", audit_score=7.0)
    _insert_job(conn, url="https://example.com/job", site="other", application_url="https://smartapply.indeed.com/apply")

    rows = indeed_resolver.fetch_candidates(limit=10, tiers=("priority", "recommended"))

    assert [row.url for row in rows] == [
        "https://www.indeed.com/viewjob?jk=priority",
        "https://example.com/job",
    ]

    refresh_rows = indeed_resolver.fetch_candidates(
        limit=10,
        tiers=("priority", "recommended"),
        refresh=True,
    )

    assert "https://www.indeed.com/viewjob?jk=company-match" in {
        row.url for row in refresh_rows
    }


def test_fetch_candidates_does_not_let_non_indeed_rows_crowd_out_indeed(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(indeed_resolver, "get_connection", lambda: conn)

    for idx in range(10):
        _insert_job(
            conn,
            url=f"https://company.example/jobs/{idx}",
            site="greenhouse",
            audit_score=99.0 - idx,
        )
    _insert_job(
        conn,
        url="https://www.indeed.com/viewjob?jk=real",
        site="indeed",
        audit_score=1.0,
    )

    rows = indeed_resolver.fetch_candidates(limit=1, tiers=("priority", "recommended"))

    assert [row.url for row in rows] == ["https://www.indeed.com/viewjob?jk=real"]


def test_fetch_candidates_pages_past_path_shadow_indeed_false_positives(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(indeed_resolver, "get_connection", lambda: conn)

    for idx in range(60):
        _insert_job(
            conn,
            url=f"https://company.example/redirect/www.indeed.com/{idx}",
            site="greenhouse",
            audit_score=99.0 - idx,
        )
    _insert_job(
        conn,
        url="https://www.indeed.com/viewjob?jk=real",
        site="indeed",
        audit_score=1.0,
    )

    rows = indeed_resolver.fetch_candidates(limit=1, tiers=("priority", "recommended"))

    assert [row.url for row in rows] == ["https://www.indeed.com/viewjob?jk=real"]


def test_run_resolver_records_external_application_url_without_browser(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(indeed_resolver, "get_connection", lambda: conn)
    _insert_job(
        conn,
        url="https://www.indeed.com/viewjob?jk=external",
        application_url="https://jobs.ashbyhq.com/acme/123",
    )

    summary = indeed_resolver.run_resolver(indeed_resolver.IndeedResolverOptions(limit=10))

    row = conn.execute(
        """
        SELECT application_url, apply_url_resolution_strategy, apply_url_resolution_source,
               apply_url_resolution_error, apply_url_resolution_attempts, liveness_status
          FROM jobs WHERE url = 'https://www.indeed.com/viewjob?jk=external'
        """
    ).fetchone()

    assert summary.considered == 1
    assert summary.counts == {"resolved_offsite": 1}
    assert row["application_url"] == "https://jobs.ashbyhq.com/acme/123"
    assert row["apply_url_resolution_strategy"] == "indeed_deterministic"
    assert row["apply_url_resolution_source"] == "resolved_offsite"
    assert row["apply_url_resolution_error"] is None
    assert row["apply_url_resolution_attempts"] == 1
    assert row["liveness_status"] == "live"


def test_run_resolver_records_hosted_indeed_apply_without_external_url(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(indeed_resolver, "get_connection", lambda: conn)
    _insert_job(
        conn,
        url="https://www.indeed.com/viewjob?jk=hosted",
        application_url="https://smartapply.indeed.com/apply?jk=hosted",
    )

    summary = indeed_resolver.run_resolver(indeed_resolver.IndeedResolverOptions(limit=10))

    row = conn.execute(
        """
        SELECT application_url, apply_url_resolution_strategy, apply_url_resolution_source,
               apply_url_resolution_error, apply_url_resolution_attempts, liveness_status
          FROM jobs WHERE url = 'https://www.indeed.com/viewjob?jk=hosted'
        """
    ).fetchone()

    assert summary.counts == {"hosted_apply": 1}
    assert row["application_url"] == "https://smartapply.indeed.com/apply?jk=hosted"
    assert row["apply_url_resolution_strategy"] == "indeed_deterministic"
    assert row["apply_url_resolution_source"] == "hosted_apply"
    assert row["apply_url_resolution_error"] is None
    assert row["apply_url_resolution_attempts"] == 1
    assert row["liveness_status"] == "live"


def test_run_resolver_dry_run_does_not_write(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(indeed_resolver, "get_connection", lambda: conn)
    _insert_job(
        conn,
        url="https://www.indeed.com/viewjob?jk=dry",
        application_url="https://jobs.lever.co/acme/456",
    )

    summary = indeed_resolver.run_resolver(
        indeed_resolver.IndeedResolverOptions(limit=10, dry_run=True)
    )

    row = conn.execute(
        """
        SELECT apply_url_resolution_strategy, apply_url_resolution_attempts
          FROM jobs WHERE url = 'https://www.indeed.com/viewjob?jk=dry'
        """
    ).fetchone()

    assert summary.dry_run is True
    assert summary.considered == 1
    assert summary.counts == {"resolved_offsite": 1}
    assert summary.sample_urls == ["https://jobs.lever.co/acme/456"]
    assert row["apply_url_resolution_strategy"] is None
    assert row["apply_url_resolution_attempts"] == 0
