from __future__ import annotations

from pathlib import Path

from applypilot import database
from applypilot.enrichment import detail


def test_resolve_url_handles_simplyhired_relative_jobs(monkeypatch) -> None:
    monkeypatch.setattr(detail, "_load_base_urls", lambda: {"SimplyHired": "https://www.simplyhired.com"})

    resolved = detail.resolve_url("/job/example-token", "SimplyHired")

    assert resolved == "https://www.simplyhired.com/job/example-token"


def test_resolve_url_rejects_non_navigable_fragment_links(monkeypatch) -> None:
    monkeypatch.setattr(detail, "_load_base_urls", lambda: {"Eluta": "https://www.eluta.ca"})

    assert detail.resolve_url("#!", "Eluta") is None
    assert detail.resolve_url("javascript:void(0)", "Eluta") is None


def test_resolve_all_urls_clears_invalid_navigation_error_for_repaired_rows(tmp_path: Path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, site, source_board, strategy, detail_error, detail_scraped_at
        )
        VALUES (
            '/job/example-token',
            'Chief of Staff',
            'SimplyHired',
            'css_selectors',
            'css_selectors',
            'Page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL',
            '2026-06-01T00:00:00+00:00'
        )
        """
    )
    conn.commit()
    monkeypatch.setattr(detail, "_load_base_urls", lambda: {"SimplyHired": "https://www.simplyhired.com"})

    stats = detail.resolve_all_urls(conn)

    row = conn.execute(
        """
        SELECT url, detail_error, detail_scraped_at
          FROM jobs
         WHERE title = 'Chief of Staff'
        """
    ).fetchone()
    assert stats["resolved"] == 1
    assert row["url"] == "https://www.simplyhired.com/job/example-token"
    assert row["detail_error"] is None
    assert row["detail_scraped_at"] is None
