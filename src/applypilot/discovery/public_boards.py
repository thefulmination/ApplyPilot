"""Public job-board API discovery.

These sources complement employer ATS crawls. They are useful for broadening
the ranked job pool before spending LLM calls on tailoring.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup

from applypilot import config
from applypilot.database import get_connection, init_db
from applypilot.discovery.jobspy import _load_location_config, _location_ok

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)
REQUEST_TIMEOUT = 25

DEFAULT_SOURCES = ["the_muse", "remotejobs_org", "remotive", "arbeitnow"]
DEFAULT_THE_MUSE_CATEGORIES = [
    "Business Operations",
    "Data and Analytics",
    "Product Management",
    "Project Management",
    "Sales",
]
DEFAULT_REMOTEJOBS_CATEGORIES = [
    "business",
    "data-science",
    "finance",
    "product-management",
    "project-management",
    "sales",
]
DEFAULT_REMOTIVE_CATEGORIES = [
    "Business",
    "Data",
    "Finance",
    "Product",
    "Project Management",
    "Sales",
]


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _plain_text(html: str | None) -> str | None:
    if not html:
        return None
    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or None


def _joined_names(items: list[dict] | None) -> str | None:
    if not items:
        return None
    names = [str(item.get("name", "")).strip() for item in items if item.get("name")]
    return "; ".join(names) if names else None


def _job_matches_queries(job: dict, query_terms: list[str]) -> bool:
    if not query_terms:
        return True
    text = " ".join(
        str(job.get(key) or "")
        for key in ("title", "description", "full_description")
    ).lower()
    return any(term.lower() in text for term in query_terms)


def _search_queries(cfg: dict) -> list[str]:
    terms: list[str] = []
    for item in cfg.get("queries", []) or []:
        if isinstance(item, dict):
            query = item.get("query")
        else:
            query = str(item)
        if query:
            terms.append(str(query))
    return terms


def _locations(cfg: dict) -> list[str]:
    configured = cfg.get("locations") or []
    values: list[str] = []
    for item in configured:
        if isinstance(item, dict):
            loc = item.get("location")
        else:
            loc = str(item)
        if loc:
            values.append(str(loc))
    if values:
        return values
    default_loc = (cfg.get("defaults") or {}).get("location")
    return [str(default_loc)] if default_loc else ["Remote"]


def _store_jobs(conn: sqlite3.Connection, jobs: list[dict]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at, "
                "company, source_board, full_description, application_url, detail_scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    url,
                    job.get("title"),
                    job.get("salary"),
                    job.get("description"),
                    job.get("location"),
                    job.get("company") or job.get("site"),
                    job.get("strategy"),
                    now,
                    job.get("company") or job.get("site"),
                    job.get("source_board") or job.get("site"),
                    job.get("full_description"),
                    job.get("application_url"),
                    now if job.get("full_description") else None,
                ),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


def _fetch_json(session: requests.Session, url: str, params: dict | None = None) -> dict:
    resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _from_the_muse(raw: dict) -> dict | None:
    url = (raw.get("refs") or {}).get("landing_page")
    if not url:
        return None
    full = _plain_text(raw.get("contents"))
    return {
        "url": url,
        "title": raw.get("name"),
        "salary": None,
        "description": (full or "")[:500] if full else None,
        "full_description": full,
        "location": _joined_names(raw.get("locations")),
        "site": (raw.get("company") or {}).get("name") or "The Muse",
        "company": (raw.get("company") or {}).get("name"),
        "source_board": "the_muse",
        "strategy": "public_board:the_muse",
        "application_url": url,
    }


def _discover_the_muse(
    session: requests.Session,
    cfg: dict,
    accept_locs: list[str],
    reject_locs: list[str],
) -> list[dict]:
    board_cfg = cfg.get("public_boards", {}) or {}
    categories = board_cfg.get("the_muse_categories") or DEFAULT_THE_MUSE_CATEGORIES
    max_pages = int(board_cfg.get("the_muse_max_pages", board_cfg.get("max_pages", 3)) or 3)
    per_source = int(board_cfg.get("results_per_source", 300) or 300)
    jobs: list[dict] = []

    for category in categories:
        for location in _locations(cfg):
            for page in range(1, max_pages + 1):
                data = _fetch_json(
                    session,
                    "https://www.themuse.com/api/public/jobs",
                    params={"page": page, "category": category, "location": location},
                )
                results = data.get("results") or []
                if not results:
                    break
                for raw in results:
                    job = _from_the_muse(raw)
                    if not job:
                        continue
                    if not _location_ok(job.get("location"), accept_locs, reject_locs):
                        continue
                    jobs.append(job)
                    if len(jobs) >= per_source:
                        return jobs
                if page >= int(data.get("page_count") or page):
                    break
                time.sleep(0.2)
    return jobs


def _from_remotejobs_org(raw: dict) -> dict | None:
    url = raw.get("url") or raw.get("apply_url")
    if not url:
        return None
    company = raw.get("company") or {}
    full = raw.get("description")
    return {
        "url": url,
        "title": raw.get("title"),
        "salary": raw.get("salary_text"),
        "description": (full or "")[:500] if full else None,
        "full_description": full,
        "location": raw.get("location") or "Remote",
        "site": company.get("name") or "RemoteJobs.org",
        "company": company.get("name"),
        "source_board": "remotejobs_org",
        "strategy": "public_board:remotejobs_org",
        "application_url": raw.get("apply_url") or url,
    }


def _discover_remotejobs_org(
    session: requests.Session,
    cfg: dict,
    accept_locs: list[str],
    reject_locs: list[str],
) -> list[dict]:
    board_cfg = cfg.get("public_boards", {}) or {}
    categories = board_cfg.get("remotejobs_categories") or DEFAULT_REMOTEJOBS_CATEGORIES
    max_pages = int(board_cfg.get("remotejobs_max_pages", board_cfg.get("max_pages", 3)) or 3)
    per_source = int(board_cfg.get("results_per_source", 300) or 300)
    jobs: list[dict] = []

    for category in categories:
        for offset in range(0, max_pages * 50, 50):
            data = _fetch_json(
                session,
                "https://remotejobs.org/api/v1/jobs",
                params={"limit": 50, "offset": offset, "category": category},
            )
            results = data.get("data") or data.get("jobs") or []
            if not results:
                break
            for raw in results:
                job = _from_remotejobs_org(raw)
                if not job:
                    continue
                if not _location_ok(job.get("location"), accept_locs, reject_locs):
                    continue
                jobs.append(job)
                if len(jobs) >= per_source:
                    return jobs
            if not (data.get("pagination") or {}).get("has_more"):
                break
            time.sleep(0.2)
    return jobs


def _from_remotive(raw: dict) -> dict | None:
    url = raw.get("url")
    if not url:
        return None
    full = _plain_text(raw.get("description"))
    return {
        "url": url,
        "title": raw.get("title"),
        "salary": raw.get("salary") or None,
        "description": (full or "")[:500] if full else None,
        "full_description": full,
        "location": raw.get("candidate_required_location") or "Remote",
        "site": raw.get("company_name") or "Remotive",
        "company": raw.get("company_name"),
        "source_board": "remotive",
        "strategy": "public_board:remotive",
        "application_url": url,
    }


def _discover_remotive(
    session: requests.Session,
    cfg: dict,
    accept_locs: list[str],
    reject_locs: list[str],
) -> list[dict]:
    board_cfg = cfg.get("public_boards", {}) or {}
    categories = board_cfg.get("remotive_categories") or DEFAULT_REMOTIVE_CATEGORIES
    per_source = int(board_cfg.get("results_per_source", 300) or 300)
    jobs: list[dict] = []

    for category in categories:
        data = _fetch_json(
            session,
            "https://remotive.com/api/remote-jobs",
            params={"category": category},
        )
        for raw in data.get("jobs") or []:
            job = _from_remotive(raw)
            if not job:
                continue
            if not _location_ok(job.get("location"), accept_locs, reject_locs):
                continue
            jobs.append(job)
            if len(jobs) >= per_source:
                return jobs
        time.sleep(0.2)
    return jobs


def _from_arbeitnow(raw: dict) -> dict | None:
    url = raw.get("url")
    if not url:
        return None
    full = _plain_text(raw.get("description"))
    return {
        "url": url,
        "title": raw.get("title"),
        "salary": None,
        "description": (full or "")[:500] if full else None,
        "full_description": full,
        "location": raw.get("location") or ("Remote" if raw.get("remote") else None),
        "site": raw.get("company_name") or "Arbeitnow",
        "company": raw.get("company_name"),
        "source_board": "arbeitnow",
        "strategy": "public_board:arbeitnow",
        "application_url": url,
    }


def _discover_arbeitnow(
    session: requests.Session,
    cfg: dict,
    accept_locs: list[str],
    reject_locs: list[str],
) -> list[dict]:
    board_cfg = cfg.get("public_boards", {}) or {}
    per_source = int(board_cfg.get("results_per_source", 300) or 300)
    query_terms = _search_queries(cfg)
    data = _fetch_json(session, "https://www.arbeitnow.com/api/job-board-api")
    jobs: list[dict] = []
    for raw in data.get("data") or []:
        job = _from_arbeitnow(raw)
        if not job:
            continue
        if not _location_ok(job.get("location"), accept_locs, reject_locs):
            continue
        if not _job_matches_queries(job, query_terms):
            continue
        jobs.append(job)
        if len(jobs) >= per_source:
            break
    return jobs


DISCOVERERS = {
    "the_muse": _discover_the_muse,
    "remotejobs_org": _discover_remotejobs_org,
    "remotive": _discover_remotive,
    "arbeitnow": _discover_arbeitnow,
}


def run_public_boards_discovery(cfg: dict | None = None) -> dict:
    """Run public job-board API discovery."""
    if cfg is None:
        cfg = config.load_search_config()

    board_cfg = cfg.get("public_boards", {}) or {}
    if not _truthy(board_cfg.get("enabled"), default=True):
        log.info("Public job boards disabled in search config")
        return {"new": 0, "existing": 0, "errors": 0, "sources": 0}

    init_db()
    accept_locs, reject_locs = _load_location_config(cfg)
    source_names = board_cfg.get("sources") or DEFAULT_SOURCES
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    conn = get_connection()
    total_new = 0
    total_existing = 0
    errors = 0
    seen_sources = 0

    for source in source_names:
        discoverer = DISCOVERERS.get(source)
        if not discoverer:
            log.warning("Unknown public job-board source: %s", source)
            continue
        seen_sources += 1
        try:
            jobs = discoverer(session, cfg, accept_locs, reject_locs)
            new, existing = _store_jobs(conn, jobs)
            total_new += new
            total_existing += existing
            log.info("[public:%s] %d kept -> %d new, %d dupes", source, len(jobs), new, existing)
        except Exception as e:
            errors += 1
            log.error("[public:%s] failed: %s", source, e)

    return {
        "new": total_new,
        "existing": total_existing,
        "errors": errors,
        "sources": seen_sources,
    }
