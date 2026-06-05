"""Workday ATS direct API scraper: searches employer career portals.

Scrapes Workday-powered career sites (TD, RBC, NVIDIA, Salesforce, etc.)
via the undocumented CXS JSON API. Zero LLM, zero browser -- pure HTTP.

Employer registry is loaded from config/employers.yaml instead of being
hardcoded. Supports sequential search + detail fetching with proxy.
"""

import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import yaml

from applypilot import config
from applypilot.config import CONFIG_DIR
from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT = 20
DEFAULT_REQUEST_RETRIES = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 2.0
DEFAULT_MAX_PAGES = 10
DEFAULT_PAGE_SIZE = 20
RETRY_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}

WORKDAY_REQUEST_TIMEOUT = DEFAULT_REQUEST_TIMEOUT
WORKDAY_REQUEST_RETRIES = DEFAULT_REQUEST_RETRIES
WORKDAY_RETRY_BACKOFF_SECONDS = DEFAULT_RETRY_BACKOFF_SECONDS
WORKDAY_MAX_PAGES = DEFAULT_MAX_PAGES


# -- Employer registry from YAML --------------------------------------------

def _load_employer_file(path) -> dict:
    """Load a Workday employer registry file."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as e:
        log.warning("Could not read Workday employer registry %s: %s", path, e)
        return {}
    except yaml.YAMLError as e:
        log.warning("Invalid Workday employer registry %s: %s", path, e)
        return {}
    return data.get("employers", {}) or {}


def load_employers() -> dict:
    """Load Workday employer registry from package config plus local overrides."""
    path = CONFIG_DIR / "employers.yaml"
    employers = _load_employer_file(path)
    if not employers:
        log.warning("employers.yaml not found at %s", path)

    local_path = config.APP_DIR / "workday_employers.yaml"
    local_employers = _load_employer_file(local_path)
    if local_employers:
        employers.update(local_employers)
        log.info("Loaded %d local Workday employers from %s", len(local_employers), local_path)

    return employers


def _clean_company_name(raw: str) -> str | None:
    """Normalize one company-watchlist line for matching."""
    name = raw.strip()
    if not name:
        return None
    name = re.sub(r"\s+#.*$", "", name).strip()
    name = re.sub(
        r"\s+\((?:partially visible|partially cut off|logo)\)\s*$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()
    name = re.sub(r"\s+", " ", name)
    return name if len(name) >= 2 else None


def _company_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _truthy_config(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _cfg_float(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _apply_runtime_config(search_cfg: dict) -> None:
    """Apply Workday HTTP timeout/retry settings from searches.yaml."""
    global WORKDAY_REQUEST_TIMEOUT
    global WORKDAY_REQUEST_RETRIES
    global WORKDAY_RETRY_BACKOFF_SECONDS
    global WORKDAY_MAX_PAGES

    WORKDAY_REQUEST_TIMEOUT = _cfg_int(
        search_cfg, "workday_request_timeout", DEFAULT_REQUEST_TIMEOUT
    )
    WORKDAY_REQUEST_RETRIES = _cfg_int(
        search_cfg, "workday_request_retries", DEFAULT_REQUEST_RETRIES
    )
    WORKDAY_RETRY_BACKOFF_SECONDS = _cfg_float(
        search_cfg, "workday_retry_backoff_seconds", DEFAULT_RETRY_BACKOFF_SECONDS
    )
    WORKDAY_MAX_PAGES = _cfg_int(search_cfg, "workday_max_pages", DEFAULT_MAX_PAGES)


def _load_company_watchlist(search_cfg: dict) -> list[str] | None:
    """Load the optional Workday company watchlist.

    Returns None when filtering is disabled, and a list when enabled.
    """
    if not _truthy_config(search_cfg.get("workday_company_watchlist_enabled", False)):
        return None

    hiring_cfg = search_cfg.get("hiring_cafe", {}) or {}
    path_value = (
        search_cfg.get("workday_company_watchlist_path")
        or search_cfg.get("company_watchlist_path")
        or hiring_cfg.get("company_watchlist_path")
    )
    if not path_value:
        log.warning("Workday company watchlist is enabled, but no path is configured")
        return []

    path = Path(path_value).expanduser()
    if not path.exists():
        log.warning("Workday company watchlist not found: %s", path)
        return []

    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError as e:
        log.warning("Workday company watchlist could not be read: %s", e)
        return []

    companies: list[str] = []
    seen: set[str] = set()
    for line in lines:
        company = _clean_company_name(line)
        if not company:
            continue
        key = _company_key(company)
        if not key or key in seen:
            continue
        seen.add(key)
        companies.append(company)

    log.info("Workday company watchlist: %d companies from %s", len(companies), path)
    return companies


def _matches_company_watchlist(employer_key: str, employer: dict, company_keys: set[str]) -> bool:
    candidates = {
        _company_key(employer_key),
        _company_key(str(employer.get("name", ""))),
    }
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in company_keys:
            return True
        if len(candidate) >= 5 and any(
            len(company_key) >= 5 and (candidate in company_key or company_key in candidate)
            for company_key in company_keys
        ):
            return True
    return False


def _filter_employers_by_company_watchlist(employers: dict, search_cfg: dict) -> dict:
    companies = _load_company_watchlist(search_cfg)
    if companies is None:
        return employers
    if not companies:
        log.warning("Workday company watchlist matched no companies; keeping all configured employers")
        return employers

    company_keys = {_company_key(company) for company in companies if _company_key(company)}
    filtered = {
        key: employer
        for key, employer in employers.items()
        if _matches_company_watchlist(key, employer, company_keys)
    }

    if not filtered:
        log.warning("Workday company watchlist matched 0 configured employers; keeping all configured employers")
        return employers

    names = ", ".join(emp.get("name", key) for key, emp in list(filtered.items())[:20])
    if len(filtered) > 20:
        names += ", ..."
    log.info(
        "Workday company watchlist matched %d/%d configured employers: %s",
        len(filtered),
        len(employers),
        names,
    )
    return filtered


# -- Location filtering from search config -----------------------------------

def _load_location_filter(search_cfg: dict | None = None):
    """Load location accept/reject lists from search config."""
    if search_cfg is None:
        search_cfg = config.load_search_config()

    location_cfg = search_cfg.get("location", {}) or {}
    accept = search_cfg.get("location_accept") or location_cfg.get("accept_patterns", [])
    reject = search_cfg.get("location_reject_non_remote") or location_cfg.get("reject_patterns", [])
    return accept, reject


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Check if a job location passes the user's location filter."""
    if not location:
        return True

    loc = location.lower()

    if any(r in loc for r in ("remote", "anywhere", "work from home", "wfh", "distributed")):
        return True

    for r in reject:
        if r.lower() in loc:
            return False

    for a in accept:
        if a.lower() in loc:
            return True

    return False


# -- HTML stripper -----------------------------------------------------------

class _HTMLStripper(HTMLParser):
    """Strip HTML tags, keep text content."""

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False
        elif tag in ("p", "div", "li", "tr"):
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"[^\S\n]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def strip_html(html: str) -> str:
    """Convert HTML to plain text."""
    if not html:
        return ""
    stripper = _HTMLStripper()
    stripper.feed(html)
    return stripper.get_text()


# -- Proxy -------------------------------------------------------------------

_opener = None


def setup_proxy(proxy_str: str | None) -> None:
    """Configure a global urllib opener with proxy support."""
    global _opener
    if not proxy_str:
        _opener = urllib.request.build_opener()
        return

    parts = proxy_str.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        proxy_url = f"http://{user}:{passwd}@{host}:{port}"
    elif len(parts) == 2:
        proxy_url = f"http://{parts[0]}:{parts[1]}"
    else:
        log.warning("Proxy format not recognized: %s (expected host:port:user:pass or host:port)", proxy_str)
        _opener = urllib.request.build_opener()
        return

    proxy_handler = urllib.request.ProxyHandler({
        "http": proxy_url,
        "https": proxy_url,
    })
    _opener = urllib.request.build_opener(proxy_handler)
    log.info("Proxy configured: %s:%s", parts[0], parts[1])


def _urlopen(req, timeout=None):
    """Open a URL using the configured opener (with or without proxy)."""
    timeout = WORKDAY_REQUEST_TIMEOUT if timeout is None else timeout
    if _opener:
        return _opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _request_json(req, label: str) -> dict:
    """Open a Workday request with retry/backoff for transient failures."""
    last_error: Exception | None = None
    attempts = max(1, WORKDAY_REQUEST_RETRIES + 1)
    for attempt in range(attempts):
        try:
            with _urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_error = e
            retryable = e.code in RETRY_STATUS_CODES
            if not retryable or attempt >= attempts - 1:
                raise
        except Exception as e:
            last_error = e
            if attempt >= attempts - 1:
                raise

        wait = min(WORKDAY_RETRY_BACKOFF_SECONDS * (2 ** attempt), 30)
        log.warning(
            "%s request failed: %s; retrying in %.1fs (%d/%d)",
            label,
            last_error,
            wait,
            attempt + 1,
            WORKDAY_REQUEST_RETRIES,
        )
        time.sleep(wait)

    raise RuntimeError(str(last_error or "Workday request failed"))


# -- Workday API -------------------------------------------------------------

def workday_search(employer: dict, search_text: str, limit: int = 20, offset: int = 0) -> dict:
    """Search jobs via Workday CXS API. Returns JSON with total + jobPostings."""
    url = f"{employer['base_url']}/wday/cxs/{employer['tenant']}/{employer['site_id']}/jobs"
    payload = json.dumps({
        "appliedFacets": {},
        "limit": limit,
        "offset": offset,
        "searchText": search_text,
    }).encode()

    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

    return _request_json(req, f"{employer['name']} search")


def workday_detail(employer: dict, external_path: str) -> dict:
    """Fetch full job detail via Workday CXS API."""
    url = f"{employer['base_url']}/wday/cxs/{employer['tenant']}/{employer['site_id']}{external_path}"

    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

    return _request_json(req, f"{employer['name']} detail")


# -- Search + paginate -------------------------------------------------------

def search_employer(
    employer_key: str,
    employer: dict,
    search_text: str,
    location_filter: bool = True,
    max_results: int = 0,
    max_pages: int | None = None,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
) -> list[dict]:
    """Search an employer, paginate through all results, optionally filter by location."""
    log.info("%s: searching \"%s\"...", employer["name"], search_text)

    all_jobs: list[dict] = []
    offset = 0
    page_size = DEFAULT_PAGE_SIZE
    page_cap = max_pages if max_pages is not None else WORKDAY_MAX_PAGES
    total = None

    while True:
        try:
            data = workday_search(employer, search_text, limit=page_size, offset=offset)
        except Exception as e:
            log.error("%s: API error at offset %d: %s", employer["name"], offset, e)
            break

        if total is None:
            total = data.get("total", 0)
            log.info("%s: %d total results", employer["name"], total)

        postings = data.get("jobPostings", [])
        if not postings:
            break

        for j in postings:
            loc = j.get("locationsText", "")
            if location_filter and accept_locs is not None and reject_locs is not None:
                if not _location_ok(loc, accept_locs, reject_locs):
                    continue

            all_jobs.append({
                "title": j.get("title", ""),
                "location": loc,
                "posted": j.get("postedOn", ""),
                "external_path": j.get("externalPath", ""),
                "employer_key": employer_key,
                "employer_name": employer["name"],
            })

        offset += page_size
        page_num = offset // page_size
        if offset >= total:
            break
        if page_cap > 0 and page_num >= page_cap:
            log.info("%s: capped at %d pages (%d results scanned)", employer["name"], page_cap, offset)
            break
        if max_results and len(all_jobs) >= max_results:
            all_jobs = all_jobs[:max_results]
            break

    log.info("%s: %d jobs found%s", employer["name"], len(all_jobs),
             " (filtered)" if location_filter else "")
    return all_jobs


# -- Fetch details -----------------------------------------------------------

def _fetch_one_detail(employer: dict, job: dict) -> dict:
    """Fetch detail for a single job."""
    try:
        detail = workday_detail(employer, job["external_path"])
        info = detail.get("jobPostingInfo", {})

        raw_desc = info.get("jobDescription", "")
        job["full_description"] = strip_html(raw_desc)
        job["apply_url"] = info.get("externalUrl", "")
        job["job_req_id"] = info.get("jobReqId", "")
        job["time_type"] = info.get("timeType", "")
        job["remote_type"] = info.get("remoteType", "")

    except Exception as e:
        job["full_description"] = ""
        job["apply_url"] = ""
        job["detail_error"] = str(e)

    return job


def fetch_details(employer: dict, jobs: list[dict]) -> list[dict]:
    """Fetch full description + apply URL for each job sequentially."""
    log.info("%s: fetching details for %d jobs...", employer["name"], len(jobs))

    completed = 0
    errors = 0
    t0 = time.time()

    for job in jobs:
        _fetch_one_detail(employer, job)
        completed += 1
        if "detail_error" in job:
            errors += 1

        if completed % 20 == 0 or completed == len(jobs):
            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            log.info("%s: %d/%d (%d errors) [%.1f jobs/sec]",
                     employer["name"], completed, len(jobs), errors, rate)

    elapsed = time.time() - t0
    log.info("%s: done in %.1fs (%.1f jobs/sec)", employer["name"], elapsed, len(jobs) / elapsed if elapsed > 0 else 0)
    return jobs


# -- DB storage --------------------------------------------------------------

def store_results(conn: sqlite3.Connection, jobs: list[dict], employers: dict) -> tuple[int, int]:
    """Store corporate jobs in DB. Returns (new, existing)."""
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("apply_url", "")
        if not url:
            emp = employers.get(job.get("employer_key", ""), {})
            if emp and job.get("external_path"):
                url = f"{emp['base_url']}/{emp['site_id']}{job['external_path']}"
        if not url:
            continue

        description = job.get("full_description", "")
        short_desc = description[:500] if description else None
        full_description = description if len(description) > 200 else None
        detail_scraped_at = now if full_description else None
        detail_error = job.get("detail_error")

        site = job.get("employer_name", "Corporate")
        strategy = "workday_api"

        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                "discovered_at, company, source_board, full_description, application_url, detail_scraped_at, detail_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), None, short_desc, job.get("location"),
                 site, strategy, now, site, "workday", full_description, url, detail_scraped_at, detail_error),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


def _process_one(
    employer_key: str,
    employers: dict,
    search_text: str,
    location_filter: bool,
    accept_locs: list[str],
    reject_locs: list[str],
    max_results: int,
    max_pages: int,
) -> dict:
    """Search one employer, fetch details, store results."""
    emp = employers[employer_key]

    try:
        jobs = search_employer(
            employer_key, emp, search_text,
            location_filter=location_filter,
            max_results=max_results,
            max_pages=max_pages,
            accept_locs=accept_locs,
            reject_locs=reject_locs,
        )
    except Exception as e:
        log.error("%s: ERROR searching '%s': %s", emp["name"], search_text, e)
        return {"employer": emp["name"], "query": search_text,
                "found": 0, "new": 0, "existing": 0, "error": str(e)}

    if not jobs:
        return {"employer": emp["name"], "query": search_text,
                "found": 0, "new": 0, "existing": 0}

    try:
        jobs = fetch_details(emp, jobs)
    except Exception as e:
        log.error("%s: ERROR fetching details for '%s': %s", emp["name"], search_text, e)

    conn = get_connection()
    new, existing = store_results(conn, jobs, employers)
    log.info("%s: %d new, %d already in DB", emp["name"], new, existing)

    return {"employer": emp["name"], "query": search_text,
            "found": len(jobs), "new": new, "existing": existing}


# -- Main orchestrator -------------------------------------------------------

def scrape_employers(
    search_text: str,
    employers: dict,
    employer_keys: list[str] | None = None,
    location_filter: bool = True,
    max_results: int = 0,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    workers: int = 1,
    max_pages: int | None = None,
) -> dict:
    """Run full scrape: search -> filter -> detail -> store.

    Sequential by default. When workers > 1, processes employers in parallel
    using ThreadPoolExecutor.
    """
    if employer_keys is None:
        employer_keys = list(employers.keys())

    if accept_locs is None:
        accept_locs = []
    if reject_locs is None:
        reject_locs = []
    if max_pages is None:
        max_pages = WORKDAY_MAX_PAGES

    # Ensure DB schema
    init_db()

    total_new = 0
    total_existing = 0
    total_found = 0
    errors = 0
    t0 = time.time()

    valid_keys = [k for k in employer_keys if k in employers]

    if workers > 1 and len(valid_keys) > 1:
        # Parallel mode
        completed = 0
        with ThreadPoolExecutor(max_workers=min(workers, len(valid_keys))) as pool:
            futures = {
                pool.submit(
                    _process_one, key, employers, search_text,
                    location_filter, accept_locs, reject_locs, max_results, max_pages,
                ): key
                for key in valid_keys
            }
            for future in as_completed(futures):
                result = future.result()
                completed += 1
                total_new += result["new"]
                total_existing += result["existing"]
                total_found += result["found"]
                if "error" in result:
                    errors += 1

                if completed % 10 == 0 or completed == len(valid_keys):
                    elapsed = time.time() - t0
                    log.info("[%s] Progress: %d/%d employers (%d new, %d dupes, %d errors) [%.0fs]",
                             search_text, completed, len(valid_keys), total_new, total_existing, errors, elapsed)
    else:
        # Sequential mode (default)
        completed = 0
        for key in valid_keys:
            result = _process_one(
                key, employers, search_text,
                location_filter, accept_locs, reject_locs, max_results, max_pages,
            )
            completed += 1
            total_new += result["new"]
            total_existing += result["existing"]
            total_found += result["found"]
            if "error" in result:
                errors += 1

            if completed % 10 == 0 or completed == len(valid_keys):
                elapsed = time.time() - t0
                log.info("[%s] Progress: %d/%d employers (%d new, %d dupes, %d errors) [%.0fs]",
                         search_text, completed, len(valid_keys), total_new, total_existing, errors, elapsed)

    elapsed = time.time() - t0
    log.info("[%s] Done: %d found, %d new, %d dupes in %.0fs",
             search_text, total_found, total_new, total_existing, elapsed)

    return {"found": total_found, "new": total_new, "existing": total_existing}


# -- Public entry point ------------------------------------------------------

def run_workday_discovery(employers: dict | None = None, workers: int = 1) -> dict:
    """Main entry point for Workday-based corporate job discovery.

    Loads employer registry from config/employers.yaml (or uses the provided
    dict), then loads search queries from the user's search config to run
    a full crawl across all employers.

    Args:
        employers: Override the employer registry. If None, loads from YAML.
        workers: Number of parallel threads for employer scraping. Default 1 (sequential).

    Returns:
        Dict with stats: found, new, existing, queries.
    """
    if employers is None:
        employers = load_employers()

    search_cfg = config.load_search_config()
    _apply_runtime_config(search_cfg)
    employers = _filter_employers_by_company_watchlist(employers, search_cfg)
    if not employers:
        log.warning("No employers configured. Create config/employers.yaml or .applypilot/workday_employers.yaml.")
        return {"found": 0, "new": 0, "existing": 0, "queries": 0}

    queries_cfg = search_cfg.get("queries", [])
    accept_locs, reject_locs = _load_location_filter(search_cfg)

    configured_queries = search_cfg.get("workday_queries") or []
    if isinstance(configured_queries, str):
        configured_queries = [configured_queries]
    queries = [str(q).strip() for q in configured_queries if str(q).strip()]
    if not queries:
        # Default to tier 1-2 queries for workday scraping
        max_tier = _cfg_int(search_cfg, "workday_max_tier", 2)
        queries = [q["query"] for q in queries_cfg if q.get("tier", 99) <= max_tier]

    if not queries:
        # Fallback: use all queries
        queries = [q["query"] for q in queries_cfg]

    if not queries:
        log.warning("No search queries configured in searches.yaml.")
        return {"found": 0, "new": 0, "existing": 0, "queries": 0}

    proxy = search_cfg.get("proxy")
    if proxy:
        setup_proxy(proxy)

    location_filter = search_cfg.get("workday_location_filter", True)
    max_results = _cfg_int(search_cfg, "workday_max_results_per_employer_query", 0)
    max_pages = _cfg_int(search_cfg, "workday_max_pages", WORKDAY_MAX_PAGES)

    log.info(
        "Workday crawl: %d queries x %d employers (workers=%d, timeout=%ds, retries=%d, max_pages=%s)",
        len(queries),
        len(employers),
        workers,
        WORKDAY_REQUEST_TIMEOUT,
        WORKDAY_REQUEST_RETRIES,
        max_pages if max_pages > 0 else "unlimited",
    )

    grand_new = 0
    grand_existing = 0
    grand_found = 0

    for i, query in enumerate(queries, 1):
        log.info("Query %d/%d: \"%s\"", i, len(queries), query)
        result = scrape_employers(
            search_text=query,
            employers=employers,
            location_filter=location_filter,
            max_results=max_results,
            max_pages=max_pages,
            accept_locs=accept_locs,
            reject_locs=reject_locs,
            workers=workers,
        )
        grand_new += result["new"]
        grand_existing += result["existing"]
        grand_found += result["found"]

    log.info("Workday crawl done: %d found, %d new, %d existing across %d queries x %d employers",
             grand_found, grand_new, grand_existing, len(queries), len(employers))

    return {
        "found": grand_found,
        "new": grand_new,
        "existing": grand_existing,
        "queries": len(queries),
    }
