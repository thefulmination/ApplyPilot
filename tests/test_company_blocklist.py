from __future__ import annotations

import sqlite3

import pytest

from applypilot import config, database
from applypilot.apply import launcher as L
from applypilot.fleet import apply_home_main


@pytest.fixture(autouse=True)
def clear_sites_cache():
    config.load_sites_config.cache_clear()
    yield
    config.load_sites_config.cache_clear()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(L, "get_connection", lambda: c)
    return c


def _write_sites(tmp_path, text: str, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "sites.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    config.load_sites_config.cache_clear()


def _ins(conn, url, *, company="Acme", application_url=None, audit=8.0, title="Chief of Staff"):
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, tailored_resume_path, "
        "fit_score, audit_score, liveness_status) VALUES (?, ?, 'X', ?, ?, 'x', 8, ?, 'live')",
        (url, title, company, application_url or f"https://boards.greenhouse.io/acme/{url[-3:]}", audit),
    )
    conn.commit()


def test_load_blocked_companies_from_sites_yaml_seed():
    names, patterns = config.load_blocked_companies()

    assert "openai" in names
    assert "%openai%" in patterns


def test_load_blocked_companies_missing_section_noop(tmp_path, monkeypatch):
    _write_sites(tmp_path, "blocked:\n  sites: []\n  url_patterns: []\n", monkeypatch)

    assert config.load_blocked_companies() == (set(), [])


def test_acquire_job_blocks_company_name(conn):
    _ins(conn, "https://jobs.example.com/openai-company", company="OpenAI")

    assert L.acquire_job(min_score=7) is None
    row = conn.execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url=?",
        ("https://jobs.example.com/openai-company",),
    ).fetchone()
    assert row["apply_status"] == "blocked"
    assert row["apply_error"] == "company_blocklist"


def test_acquire_job_blocks_application_url_pattern_with_board_company(conn):
    _ins(
        conn,
        "https://hiring.cafe/viewjob/openai-board",
        company="HiringCafe",
        application_url="https://jobs.ashbyhq.com/openai/123",
    )

    assert L.acquire_job(min_score=7) is None
    row = conn.execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url=?",
        ("https://hiring.cafe/viewjob/openai-board",),
    ).fetchone()
    assert row["apply_status"] == "blocked"
    assert row["apply_error"] == "company_blocklist"


def test_acquire_job_target_url_blocks_and_returns_none(conn):
    url = "https://jobs.example.com/target-openai"
    _ins(conn, url, company=None, application_url="https://boards.greenhouse.io/openai/target")

    assert L.acquire_job(target_url=url, min_score=7) is None
    row = conn.execute("SELECT apply_status, apply_error FROM jobs WHERE url=?", (url,)).fetchone()
    assert row["apply_status"] == "blocked"
    assert row["apply_error"] == "company_blocklist"


def test_acquire_job_still_returns_non_blocked_company(conn):
    _ins(conn, "https://jobs.example.com/acme-ok", company="Acme")

    job = L.acquire_job(min_score=7)

    assert job is not None
    assert job["url"] == "https://jobs.example.com/acme-ok"


def _minimal_home_db(tmp_path) -> sqlite3.Connection:
    c = sqlite3.connect(str(tmp_path / "home.db"))
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT,
            audit_score REAL, fit_score INTEGER, liveness_status TEXT,
            apply_status TEXT, apply_error TEXT, duplicate_of_url TEXT
        );
        """
    )
    return c


def _home_job(conn, url, *, company="OpenAI", application_url=None, apply_status=None):
    conn.execute(
        "INSERT INTO jobs (url, company, title, application_url, audit_score, liveness_status, apply_status) "
        "VALUES (?, ?, 'Role', ?, 8, 'live', ?)",
        (url, company, application_url or url, apply_status),
    )
    conn.commit()


def test_blocklist_backfill_dry_run_marks_nothing(tmp_path, fleet_db):
    sq = _minimal_home_db(tmp_path)
    _home_job(sq, "https://jobs.example.com/openai-dry")

    with apply_home_main.pgqueue.connect(fleet_db) as pg:
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, status) "
                "VALUES ('pg-openai-dry', 'OpenAI', 'Role', 'https://openai.com/jobs/1', 8, 'queued')"
            )
        pg.commit()

        counts = apply_home_main.blocklist_backfill(sqlite_conn=sq, pg_conn=pg, execute=False)

        assert counts["brain_matches"] == 1
        assert counts["apply_queue_matches"] == 1
        assert sq.execute("SELECT apply_status FROM jobs").fetchone()["apply_status"] is None
        with pg.cursor() as cur:
            cur.execute("SELECT status FROM apply_queue WHERE url='pg-openai-dry'")
            assert cur.fetchone()["status"] == "queued"


def test_blocklist_backfill_execute_blocks_only_safe_rows(tmp_path, fleet_db):
    sq = _minimal_home_db(tmp_path)
    _home_job(sq, "https://jobs.example.com/openai-safe", company="OpenAI")
    _home_job(sq, "https://jobs.example.com/openai-applied", company="OpenAI", apply_status="applied")

    with apply_home_main.pgqueue.connect(fleet_db) as pg:
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, status) "
                "VALUES ('pg-openai-safe', 'OpenAI', 'Role', 'https://openai.com/jobs/1', 8, 'queued')"
            )
            cur.execute(
                "INSERT INTO linkedin_queue (url, company, title, application_url, score, status) "
                "VALUES ('li-openai-safe', 'OpenAI', 'Role', 'https://linkedin.com/jobs/1', 8, 'queued')"
            )
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, status) "
                "VALUES ('pg-openai-leased', 'OpenAI', 'Role', 'https://openai.com/jobs/2', 8, 'leased')"
            )
        pg.commit()

        counts = apply_home_main.blocklist_backfill(sqlite_conn=sq, pg_conn=pg, execute=True)

        assert counts["brain_blocked"] == 1
        assert counts["apply_queue_blocked"] == 1
        assert counts["linkedin_queue_blocked"] == 1
        safe = sq.execute(
            "SELECT apply_status, apply_error FROM jobs WHERE url='https://jobs.example.com/openai-safe'"
        ).fetchone()
        applied = sq.execute(
            "SELECT apply_status, apply_error FROM jobs WHERE url='https://jobs.example.com/openai-applied'"
        ).fetchone()
        assert safe["apply_status"] == "blocked"
        assert safe["apply_error"] == "company_blocklist"
        assert applied["apply_status"] == "applied"
        with pg.cursor() as cur:
            cur.execute("SELECT status, apply_error FROM apply_queue WHERE url='pg-openai-safe'")
            assert dict(cur.fetchone()) == {"status": "blocked", "apply_error": "company_blocklist"}
            cur.execute("SELECT status, apply_error FROM linkedin_queue WHERE url='li-openai-safe'")
            assert dict(cur.fetchone()) == {"status": "blocked", "apply_error": "company_blocklist"}
            cur.execute("SELECT status FROM apply_queue WHERE url='pg-openai-leased'")
            assert cur.fetchone()["status"] == "leased"
