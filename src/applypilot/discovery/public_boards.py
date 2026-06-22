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
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

from applypilot import config
from applypilot.database import get_connection, init_db, insert_discovered_job
from applypilot.discovery.jobspy import _load_location_config, _location_ok
from applypilot.discovery.resilience import get_board_breaker, jitter_backoff
from applypilot.discovery.schema import validate_jobs

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)
REQUEST_TIMEOUT = 25

DEFAULT_SOURCES = [
    "the_muse",
    "remotejobs_org",
    "remotive",
    "arbeitnow",
    "hacker_news",
    "yc_jobs",
    "builtin",
    "chief_of_staff_jobs",
    "remoteok",
    "himalayas",
    "jobicy",
    "weworkremotely",
]
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
DEFAULT_BUILTIN_PATHS = [
    "/jobs/remote/operations",
    "/jobs/remote/sales",
    "/jobs/remote/product",
    "/jobs/remote/data-analytics",
    "/jobs/remote/finance",
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
    for term in query_terms:
        normalized = term.lower().strip()
        if not normalized:
            continue
        if normalized in text:
            return True
        words = [word for word in re.split(r"\W+", normalized) if word]
        if words and all(word in text for word in words):
            return True
    return False


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


def _store_jobs(conn: sqlite3.Connection, jobs: list[dict], board: str = "unknown") -> tuple[int, int]:
    valid_jobs, _report = validate_jobs(jobs, board=board)
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for job in valid_jobs:
        url = job.get("url")
        if not url:
            continue
        status = insert_discovered_job(
            conn,
            {**job, "detail_scraped_at": now if job.get("full_description") else None},
            site=job.get("company") or job.get("site"),
            strategy=job.get("strategy"),
            source_board=job.get("source_board") or job.get("site"),
            discovered_at=now,
        )
        if status == "new":
            new += 1
        elif status in {"existing", "duplicate"}:
            existing += 1

    conn.commit()
    return new, existing


_FETCH_RETRIES = 3
_FETCH_BACKOFF = 2.0
_FETCH_RETRY_STATUS = {408, 429, 500, 502, 503, 504}


def _request_with_retries(
    session: requests.Session,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
) -> requests.Response:
    """GET with bounded retry on transient failures (timeouts, 429, 5xx).

    Uses jittered exponential backoff instead of fixed linear sleeps to avoid
    synchronised retry bursts when multiple boards are slow simultaneously.
    Honors Retry-After. Raises the last error after retries are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _FETCH_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT, headers=headers)
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            if attempt < _FETCH_RETRIES:
                time.sleep(jitter_backoff(attempt, base=_FETCH_BACKOFF))
                continue
            raise
        if resp.status_code in _FETCH_RETRY_STATUS and attempt < _FETCH_RETRIES:
            ra = resp.headers.get("Retry-After")
            try:
                explicit_wait = float(ra) if ra else None
            except (TypeError, ValueError):
                explicit_wait = None
            wait = explicit_wait if explicit_wait is not None else jitter_backoff(attempt, base=_FETCH_BACKOFF)
            time.sleep(min(max(_FETCH_BACKOFF, wait), 60.0))
            continue
        resp.raise_for_status()
        return resp
    if last_exc:
        raise last_exc
    raise RuntimeError(f"request failed after {_FETCH_RETRIES} attempts: {url}")


def _fetch_json(session: requests.Session, url: str, params: dict | None = None) -> dict:
    resp = _request_with_retries(session, url, params=params, headers={"Accept": "application/json"})
    return resp.json()


def _fetch_html(session: requests.Session, url: str, params: dict | None = None) -> str:
    resp = _request_with_retries(
        session, url, params=params, headers={"Accept": "text/html,application/xhtml+xml"}
    )
    return resp.text


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


def _from_simple_link(
    *,
    url: str,
    title: str,
    source_board: str,
    site: str,
    company: str | None = None,
    location: str | None = None,
    description: str | None = None,
) -> dict | None:
    title = re.sub(r"\s+", " ", title).strip()
    if not url or not title:
        return None
    return {
        "url": url,
        "title": title,
        "salary": None,
        "description": (description or title)[:500],
        "full_description": description,
        "location": location,
        "site": company or site,
        "company": company or site,
        "source_board": source_board,
        "strategy": f"public_board:{source_board}",
        "application_url": url,
    }


def _discover_hacker_news(
    session: requests.Session,
    cfg: dict,
    accept_locs: list[str],
    reject_locs: list[str],
) -> list[dict]:
    board_cfg = cfg.get("public_boards", {}) or {}
    per_source = int(board_cfg.get("hacker_news_results", board_cfg.get("results_per_source", 300)) or 300)
    query_terms = _search_queries(cfg)
    html = _fetch_html(session, "https://news.ycombinator.com/jobs")
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []
    seen: set[str] = set()

    for titleline in soup.select(".titleline"):
        link = titleline.find("a", href=True)
        if not link:
            continue
        href = urljoin("https://news.ycombinator.com/", link["href"])
        title = link.get_text(" ", strip=True)
        if href in seen:
            continue
        seen.add(href)
        job = _from_simple_link(
            url=href,
            title=title,
            source_board="hacker_news",
            site="Hacker News Jobs",
            company="Hacker News Jobs",
            location="Remote",
            description=title,
        )
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


def _discover_yc_jobs(
    session: requests.Session,
    cfg: dict,
    accept_locs: list[str],
    reject_locs: list[str],
) -> list[dict]:
    board_cfg = cfg.get("public_boards", {}) or {}
    per_source = int(board_cfg.get("yc_jobs_results", board_cfg.get("results_per_source", 300)) or 300)
    query_terms = _search_queries(cfg)
    jobs: list[dict] = []
    seen: set[str] = set()

    for query in query_terms or [""]:
        url = "https://www.ycombinator.com/jobs"
        if query:
            url = f"{url}?query={quote(query.lower())}"
        html = _fetch_html(session, url)
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if not re.search(r"/companies/[^/]+/jobs/", href):
                continue
            full_url = urljoin("https://www.ycombinator.com/", href)
            if full_url in seen:
                continue
            seen.add(full_url)
            title = link.get_text(" ", strip=True)
            company_match = re.search(r"/companies/([^/]+)/jobs/", href)
            company = (company_match.group(1).replace("-", " ").title() if company_match else "Y Combinator")
            job = _from_simple_link(
                url=full_url,
                title=title,
                source_board="yc_jobs",
                site=company,
                company=company,
                location="Remote",
                description=f"{title} at {company}",
            )
            if not job:
                continue
            if not _location_ok(job.get("location"), accept_locs, reject_locs):
                continue
            if not _job_matches_queries(job, query_terms):
                continue
            jobs.append(job)
            if len(jobs) >= per_source:
                return jobs
        time.sleep(0.2)
    return jobs


def _discover_builtin(
    session: requests.Session,
    cfg: dict,
    accept_locs: list[str],
    reject_locs: list[str],
) -> list[dict]:
    board_cfg = cfg.get("public_boards", {}) or {}
    paths = board_cfg.get("builtin_paths") or DEFAULT_BUILTIN_PATHS
    per_source = int(board_cfg.get("builtin_results", board_cfg.get("results_per_source", 300)) or 300)
    query_terms = _search_queries(cfg)
    jobs: list[dict] = []
    seen: set[str] = set()

    for path in paths:
        html = _fetch_html(session, urljoin("https://builtin.com/", path))
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if not re.match(r"^/job/[^/]+/\d+", href):
                continue
            full_url = urljoin("https://builtin.com/", href)
            if full_url in seen:
                continue
            seen.add(full_url)
            title = link.get_text(" ", strip=True)
            job = _from_simple_link(
                url=full_url,
                title=title,
                source_board="builtin",
                site="Built In",
                company="Built In",
                location="Remote",
                description=title,
            )
            if not job:
                continue
            if not _location_ok(job.get("location"), accept_locs, reject_locs):
                continue
            if not _job_matches_queries(job, query_terms):
                continue
            jobs.append(job)
            if len(jobs) >= per_source:
                return jobs
        time.sleep(0.2)
    return jobs


def _discover_chief_of_staff_jobs(
    session: requests.Session,
    cfg: dict,
    accept_locs: list[str],
    reject_locs: list[str],
) -> list[dict]:
    board_cfg = cfg.get("public_boards", {}) or {}
    per_source = int(board_cfg.get("chief_of_staff_jobs_results", board_cfg.get("results_per_source", 300)) or 300)
    html = _fetch_html(session, "https://www.chiefofstaffjob.com/")
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []
    seen: set[str] = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not href.startswith("/jobs/"):
            continue
        full_url = urljoin("https://www.chiefofstaffjob.com/", href)
        if full_url in seen:
            continue
        seen.add(full_url)
        text = link.get_text(" ", strip=True)
        location = "Remote" if re.search(r"\bremote\b", text, re.I) else text
        job = _from_simple_link(
            url=full_url,
            title=text,
            source_board="chief_of_staff_jobs",
            site="ChiefOfStaffJob.com",
            company="ChiefOfStaffJob.com",
            location=location,
            description=text,
        )
        if not job:
            continue
        if not _location_ok(job.get("location"), accept_locs, reject_locs):
            continue
        jobs.append(job)
        if len(jobs) >= per_source:
            return jobs
    return jobs


# -- Remote-board feeds: RemoteOK / Himalayas / Jobicy (JSON) + WeWorkRemotely (RSS) --

def _fmt_range(lo: Any, hi: Any, prefix: str = "$") -> str | None:
    """Format a (min,max) pair as a salary string, or None if not numeric."""
    try:
        if lo and hi:
            return f"{prefix}{int(lo):,}-{prefix}{int(hi):,}"
    except (TypeError, ValueError):
        pass
    return None


def _from_remoteok(raw: dict) -> dict | None:
    # RemoteOK's /api returns a list whose first element is a legal notice
    # (no id/position) -- skip it.
    if not isinstance(raw, dict) or not raw.get("id") or not raw.get("position"):
        return None
    url = raw.get("url") or raw.get("apply_url")
    if not url:
        return None
    full = _plain_text(raw.get("description"))
    company = raw.get("company")
    return {
        "url": url,
        "title": raw.get("position"),
        "salary": _fmt_range(raw.get("salary_min"), raw.get("salary_max")),
        "description": (full or "")[:500] if full else None,
        "full_description": full,
        "location": raw.get("location") or "Remote",
        "site": company or "RemoteOK",
        "company": company,
        "source_board": "remoteok",
        "strategy": "public_board:remoteok",
        "application_url": raw.get("apply_url") or url,
    }


def _discover_remoteok(session, cfg, accept_locs, reject_locs) -> list[dict]:
    board_cfg = cfg.get("public_boards", {}) or {}
    per_source = int(board_cfg.get("results_per_source", 300) or 300)
    query_terms = _search_queries(cfg)
    data = _fetch_json(session, "https://remoteok.com/api")
    items = data if isinstance(data, list) else []
    jobs: list[dict] = []
    for raw in items:
        job = _from_remoteok(raw)
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


def _from_himalayas(raw: dict) -> dict | None:
    url = raw.get("guid") or raw.get("applicationLink") or raw.get("url")
    if not url:
        return None
    full = _plain_text(raw.get("description")) or _plain_text(raw.get("excerpt"))
    locs = raw.get("locationRestrictions")
    location = ", ".join(str(x) for x in locs) if isinstance(locs, list) and locs else "Remote"
    company = raw.get("companyName")
    return {
        "url": url,
        "title": raw.get("title"),
        "salary": _fmt_range(raw.get("minSalary"), raw.get("maxSalary")),
        "description": (full or "")[:500] if full else None,
        "full_description": full,
        "location": location,
        "site": company or "Himalayas",
        "company": company,
        "source_board": "himalayas",
        "strategy": "public_board:himalayas",
        "application_url": raw.get("applicationLink") or url,
    }


def _discover_himalayas(session, cfg, accept_locs, reject_locs) -> list[dict]:
    board_cfg = cfg.get("public_boards", {}) or {}
    per_source = int(board_cfg.get("results_per_source", 300) or 300)
    query_terms = _search_queries(cfg)
    data = _fetch_json(session, "https://himalayas.app/jobs/api", params={"limit": min(per_source, 100)})
    jobs: list[dict] = []
    for raw in data.get("jobs") or []:
        job = _from_himalayas(raw)
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


def _from_jobicy(raw: dict) -> dict | None:
    url = raw.get("url")
    if not url:
        return None
    full = _plain_text(raw.get("jobDescription")) or _plain_text(raw.get("jobExcerpt"))
    company = raw.get("companyName")
    return {
        "url": url,
        "title": raw.get("jobTitle"),
        "salary": _fmt_range(raw.get("annualSalaryMin"), raw.get("annualSalaryMax"),
                             prefix=f"{raw.get('salaryCurrency') or 'USD'} ".replace("USD ", "$")),
        "description": (full or "")[:500] if full else None,
        "full_description": full,
        "location": raw.get("jobGeo") or "Remote",
        "site": company or "Jobicy",
        "company": company,
        "source_board": "jobicy",
        "strategy": "public_board:jobicy",
        "application_url": url,
    }


def _discover_jobicy(session, cfg, accept_locs, reject_locs) -> list[dict]:
    board_cfg = cfg.get("public_boards", {}) or {}
    per_source = int(board_cfg.get("results_per_source", 300) or 300)
    query_terms = _search_queries(cfg)
    data = _fetch_json(session, "https://jobicy.com/api/v2/remote-jobs", params={"count": min(per_source, 50)})
    jobs: list[dict] = []
    for raw in data.get("jobs") or []:
        job = _from_jobicy(raw)
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


_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_WWR_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S | re.I)
DEFAULT_WWR_CATEGORIES = [
    "remote-management-and-finance-jobs",
    "remote-business-jobs",
    "remote-sales-and-marketing-jobs",
    "remote-product-jobs",
]


def _rss_field(block: str, tag: str) -> str | None:
    """Extract one RSS element's text (CDATA-aware) without an XML parser
    (BeautifulSoup's html.parser treats <link> as void, which breaks RSS)."""
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S | re.I)
    if not m:
        return None
    val = m.group(1)
    cd = _CDATA_RE.search(val)
    if cd:
        val = cd.group(1)
    return val.strip() or None


def _from_wwr_block(block: str) -> dict | None:
    url = _rss_field(block, "link")
    raw_title = _rss_field(block, "title")
    if not url or not raw_title:
        return None
    # WeWorkRemotely titles are "Company: Position".
    company, sep, position = raw_title.partition(":")
    company, position = company.strip(), position.strip()
    if not sep or not position:
        position, company = raw_title.strip(), ""
    full = _plain_text(_rss_field(block, "description"))
    return {
        "url": url,
        "title": position,
        "salary": None,
        "description": (full or position)[:500],
        "full_description": full,
        "location": _rss_field(block, "region") or "Remote",
        "site": company or "WeWorkRemotely",
        "company": company or None,
        "source_board": "weworkremotely",
        "strategy": "public_board:weworkremotely",
        "application_url": url,
    }


def _discover_weworkremotely(session, cfg, accept_locs, reject_locs) -> list[dict]:
    board_cfg = cfg.get("public_boards", {}) or {}
    per_source = int(board_cfg.get("results_per_source", 300) or 300)
    categories = board_cfg.get("weworkremotely_categories") or DEFAULT_WWR_CATEGORIES
    query_terms = _search_queries(cfg)
    jobs: list[dict] = []
    seen: set[str] = set()
    for category in categories:
        try:
            xml = _fetch_html(session, f"https://weworkremotely.com/categories/{category}.rss")
        except Exception as e:
            log.warning("[public:weworkremotely] %s failed: %s", category, e)
            continue
        for block in _WWR_ITEM_RE.findall(xml):
            job = _from_wwr_block(block)
            if not job or job["url"] in seen:
                continue
            seen.add(job["url"])
            if not _location_ok(job.get("location"), accept_locs, reject_locs):
                continue
            if not _job_matches_queries(job, query_terms):
                continue
            jobs.append(job)
            if len(jobs) >= per_source:
                return jobs
        time.sleep(0.2)
    return jobs


DISCOVERERS = {
    "the_muse": _discover_the_muse,
    "remotejobs_org": _discover_remotejobs_org,
    "remotive": _discover_remotive,
    "arbeitnow": _discover_arbeitnow,
    "hacker_news": _discover_hacker_news,
    "yc_jobs": _discover_yc_jobs,
    "builtin": _discover_builtin,
    "chief_of_staff_jobs": _discover_chief_of_staff_jobs,
    "remoteok": _discover_remoteok,
    "himalayas": _discover_himalayas,
    "jobicy": _discover_jobicy,
    "weworkremotely": _discover_weworkremotely,
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
        breaker = get_board_breaker(f"public:{source}")
        if breaker.is_open():
            log.warning("[public:%s] circuit open — skipping this run", source)
            errors += 1
            continue
        try:
            jobs = discoverer(session, cfg, accept_locs, reject_locs)
            new, existing = _store_jobs(conn, jobs, board=source)
            total_new += new
            total_existing += existing
            log.info("[public:%s] %d kept -> %d new, %d dupes", source, len(jobs), new, existing)
            breaker.record_success()
        except Exception as e:
            errors += 1
            breaker.record_failure()
            log.error("[public:%s] failed: %s", source, e)

    return {
        "new": total_new,
        "existing": total_existing,
        "errors": errors,
        "sources": seen_sources,
    }
