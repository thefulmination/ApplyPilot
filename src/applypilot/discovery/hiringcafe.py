"""HiringCafe job discovery.

HiringCafe exposes server-rendered search pages with structured job data in
Next.js page props. This scraper reads those props directly, so it does not
need the LLM-powered smart extractor.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from applypilot import config
from applypilot.database import get_connection, init_db, insert_discovered_job
from applypilot.discovery.jobspy import _load_location_config, _location_ok

log = logging.getLogger(__name__)

BASE_URL = "https://hiring.cafe"
REQUEST_TIMEOUT = 30
DEFAULT_MAX_PAGES = 2
DEFAULT_COMPANY_MAX_PAGES = 1
DEFAULT_COMPANY_RESULTS_PER_SITE = 20
DEFAULT_REQUEST_DELAY_SECONDS = 0.35
DEFAULT_COMPANY_REQUEST_DELAY_SECONDS = 0.75
DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 90.0
DEFAULT_MAX_CONSECUTIVE_RATE_LIMITS = 3
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

STATE_NAMES = {
    "al": "alabama",
    "ak": "alaska",
    "az": "arizona",
    "ar": "arkansas",
    "ca": "california",
    "co": "colorado",
    "ct": "connecticut",
    "dc": "district-of-columbia",
    "de": "delaware",
    "fl": "florida",
    "ga": "georgia",
    "hi": "hawaii",
    "ia": "iowa",
    "id": "idaho",
    "il": "illinois",
    "in": "indiana",
    "ks": "kansas",
    "ky": "kentucky",
    "la": "louisiana",
    "ma": "massachusetts",
    "md": "maryland",
    "me": "maine",
    "mi": "michigan",
    "mn": "minnesota",
    "mo": "missouri",
    "ms": "mississippi",
    "mt": "montana",
    "nc": "north-carolina",
    "nd": "north-dakota",
    "ne": "nebraska",
    "nh": "new-hampshire",
    "nj": "new-jersey",
    "nm": "new-mexico",
    "nv": "nevada",
    "ny": "new-york",
    "oh": "ohio",
    "ok": "oklahoma",
    "or": "oregon",
    "pa": "pennsylvania",
    "ri": "rhode-island",
    "sc": "south-carolina",
    "sd": "south-dakota",
    "tn": "tennessee",
    "tx": "texas",
    "ut": "utah",
    "va": "virginia",
    "vt": "vermont",
    "wa": "washington",
    "wi": "wisconsin",
    "wv": "west-virginia",
    "wy": "wyoming",
}


class HiringCafeRateLimitError(RuntimeError):
    """Raised when HiringCafe returns HTTP 429 and the crawl should back off."""

    def __init__(self, url: str, retry_after_seconds: float | None = None) -> None:
        self.url = url
        self.retry_after_seconds = retry_after_seconds
        suffix = f"; retry after {retry_after_seconds:g}s" if retry_after_seconds is not None else ""
        super().__init__(f"HTTP 429 Too Many Requests for {url}{suffix}")

KNOWN_LOCATION_SLUGS = {
    "remote": "united-states",
    "united states": "united-states",
    "usa": "united-states",
    "us": "united-states",
    "san francisco": "san-francisco-california",
    "san francisco ca": "san-francisco-california",
    "san francisco california": "san-francisco-california",
    "california": "california-united-states",
    "ca": "california-united-states",
}


def _slugify(text: str) -> str:
    """Convert a query/location string to HiringCafe's slug style."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-+", "-", slug)


def _location_slug(location: str | None) -> str:
    """Best-effort conversion from ApplyPilot locations to HiringCafe slugs."""
    if not location:
        return "united-states"

    normalized = re.sub(r"[^a-z0-9]+", " ", location.lower()).strip()
    normalized = re.sub(r"\b(us|usa|united states)\b", "", normalized).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    if normalized in KNOWN_LOCATION_SLUGS:
        return KNOWN_LOCATION_SLUGS[normalized]

    parts = [p.strip() for p in re.split(r"[,/]", location) if p.strip()]
    if len(parts) >= 2:
        city = parts[0]
        state_raw = parts[1].lower().strip()
        state_key = re.sub(r"[^a-z]", "", state_raw)
        state = STATE_NAMES.get(state_key) or _slugify(state_raw)
        if city and state:
            return f"{_slugify(city)}-{state}"

    return _slugify(normalized or location)


def _search_url(query: str, location: str | None) -> str:
    return f"{BASE_URL}/jobs/{_slugify(query)}/locations/{_location_slug(location)}"


def _clean_company_name(raw: str) -> str | None:
    """Normalize one company-watchlist line for use as a HiringCafe query."""
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


def _float_config(value, default: float) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _int_config(value, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None

    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def _load_company_watchlist(cfg: dict) -> list[str]:
    """Load optional company names from a user-maintained text file."""
    hiring_cfg = cfg.get("hiring_cafe", {}) or {}
    if not _truthy_config(hiring_cfg.get("company_watchlist_enabled", True)):
        return []

    path_value = hiring_cfg.get("company_watchlist_path") or cfg.get("company_watchlist_path")
    if not path_value:
        return []

    path = Path(path_value).expanduser()
    if not path.exists():
        log.warning("HiringCafe company watchlist not found: %s", path)
        return []

    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError as e:
        log.warning("HiringCafe company watchlist could not be read: %s", e)
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

    try:
        limit = int(hiring_cfg.get("company_watchlist_limit", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit > 0:
        companies = companies[:limit]

    log.info("HiringCafe company watchlist: %d companies from %s", len(companies), path)
    return companies


def _fetch_page(session: requests.Session, url: str) -> dict:
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 429:
        raise HiringCafeRateLimitError(url, _parse_retry_after(resp.headers.get("Retry-After")))
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    data_el = soup.select_one("script#__NEXT_DATA__")
    if not data_el or not data_el.string:
        raise ValueError("HiringCafe page did not include __NEXT_DATA__")

    import json
    return json.loads(data_el.string)["props"]["pageProps"]


def _plain_text(html: str | None) -> str | None:
    if not html:
        return None
    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _format_salary(processed: dict) -> str | None:
    currency = processed.get("listed_compensation_currency") or "USD"
    freq = (processed.get("listed_compensation_frequency") or "Yearly").lower()
    ranges = [
        ("yearly", processed.get("yearly_min_compensation"), processed.get("yearly_max_compensation"), "yr"),
        ("monthly", processed.get("monthly_min_compensation"), processed.get("monthly_max_compensation"), "mo"),
        ("weekly", processed.get("weekly_min_compensation"), processed.get("weekly_max_compensation"), "wk"),
        ("hourly", processed.get("hourly_min_compensation"), processed.get("hourly_max_compensation"), "hr"),
        ("daily", processed.get("daily_min_compensation"), processed.get("daily_max_compensation"), "day"),
    ]

    symbol = "$" if currency.upper() == "USD" else f"{currency} "
    for key, min_val, max_val, suffix in ranges:
        if key not in freq and not (min_val or max_val):
            continue
        if min_val and max_val:
            return f"{symbol}{int(min_val):,}-{symbol}{int(max_val):,}/{suffix}"
        if min_val:
            return f"{symbol}{int(min_val):,}+/{suffix}"
        if max_val:
            return f"up to {symbol}{int(max_val):,}/{suffix}"
    return None


def _build_description(hit: dict) -> str | None:
    job_info = hit.get("job_information") or {}
    processed = hit.get("v5_processed_job_data") or {}
    company = hit.get("enriched_company_data") or {}

    parts: list[str] = []
    summary = processed.get("requirements_summary")
    if summary:
        parts.append(f"Requirements Summary:\n{summary}")

    tools = processed.get("technical_tools") or []
    if tools:
        parts.append(f"Technical Tools Mentioned:\n{', '.join(str(t) for t in tools)}")

    activities = processed.get("role_activities") or []
    if activities:
        parts.append("Role Activities:\n" + "\n".join(f"- {a}" for a in activities))

    tagline = company.get("tagline") or processed.get("company_tagline")
    company_name = company.get("name") or processed.get("company_name")
    if company_name and tagline:
        parts.append(f"Company:\n{company_name}: {tagline}")

    raw_description = _plain_text(job_info.get("description"))
    if raw_description:
        parts.append(raw_description)

    return "\n\n".join(parts).strip() or None


def _hit_to_job(hit: dict) -> dict | None:
    if hit.get("is_expired"):
        return None

    job_info = hit.get("job_information") or {}
    processed = hit.get("v5_processed_job_data") or {}
    company = hit.get("enriched_company_data") or {}
    req_id = hit.get("requisition_id")
    if not req_id:
        return None

    location = processed.get("formatted_workplace_location")
    workplace = processed.get("workplace_type")
    if workplace and location:
        location = f"{location} ({workplace})"
    elif workplace:
        location = workplace

    return {
        "url": urljoin(BASE_URL, f"/viewjob/{req_id}"),
        "title": job_info.get("title") or processed.get("core_job_title"),
        "salary": _format_salary(processed),
        "description": processed.get("requirements_summary") or _plain_text(job_info.get("description")),
        "full_description": _build_description(hit),
        "location": location,
        "company": company.get("name") or processed.get("company_name"),
        "application_url": hit.get("apply_url"),
    }


def _store_jobs(conn: sqlite3.Connection, jobs: list[dict]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        status = insert_discovered_job(
            conn,
            {**job, "detail_scraped_at": None},
            site=job.get("company") or "HiringCafe",
            strategy="hiringcafe_next_data",
            source_board="hiringcafe",
            discovered_at=now,
        )
        if status == "new":
            new += 1
        elif status in {"existing", "duplicate"}:
            existing += 1

    conn.commit()
    return new, existing


def _run_one_search(
    session: requests.Session,
    query: str,
    location: str,
    results_per_site: int,
    max_pages: int,
    accept_locs: list[str],
    reject_locs: list[str],
) -> dict:
    base_url = _search_url(query, location)
    all_jobs: list[dict] = []
    total_seen = 0
    filtered = 0
    errors = 0
    rate_limited = False
    retry_after_seconds: float | None = None

    for page_num in range(max_pages):
        url = base_url if page_num == 0 else f"{base_url}?page={page_num}"
        try:
            page = _fetch_page(session, url)
        except HiringCafeRateLimitError as e:
            errors += 1
            rate_limited = True
            retry_after_seconds = e.retry_after_seconds
            log.warning("[HiringCafe] %s in %s page %d rate limited: %s", query, location, page_num + 1, e)
            break
        except Exception as e:
            errors += 1
            log.error("[HiringCafe] %s in %s page %d failed: %s", query, location, page_num + 1, e)
            break

        hits = page.get("ssrHits") or []
        total_seen += len(hits)
        for hit in hits:
            job = _hit_to_job(hit)
            if not job:
                continue
            if not _location_ok(job.get("location"), accept_locs, reject_locs):
                filtered += 1
                continue
            all_jobs.append(job)
            if len(all_jobs) >= results_per_site:
                break

        if len(all_jobs) >= results_per_site or page.get("ssrIsLastPage") or not hits:
            break
        time.sleep(0.5)

    if not all_jobs:
        log.info("[HiringCafe] \"%s\" in %s: %d seen, 0 kept, %d filtered", query, location, total_seen, filtered)
        return {
            "new": 0,
            "existing": 0,
            "seen": total_seen,
            "filtered": filtered,
            "errors": errors,
            "rate_limited": rate_limited,
            "retry_after_seconds": retry_after_seconds,
        }

    conn = get_connection()
    new, existing = _store_jobs(conn, all_jobs)
    log.info(
        "[HiringCafe] \"%s\" in %s: %d seen -> %d new, %d dupes, %d filtered",
        query,
        location,
        total_seen,
        new,
        existing,
        filtered,
    )
    return {
        "new": new,
        "existing": existing,
        "seen": total_seen,
        "filtered": filtered,
        "errors": errors,
        "rate_limited": rate_limited,
        "retry_after_seconds": retry_after_seconds,
    }


def run_hiringcafe_discovery(cfg: dict | None = None) -> dict:
    """Run HiringCafe discovery for configured queries and locations."""
    if cfg is None:
        cfg = config.load_search_config()

    hiring_cfg = cfg.get("hiring_cafe", {}) or {}
    if hiring_cfg.get("enabled", True) is False:
        log.info("HiringCafe disabled in search config")
        return {"new": 0, "existing": 0, "errors": 0, "queries": 0}

    locations = cfg.get("locations", [])

    search_terms: list[dict[str, str]] = []
    seen_terms: set[str] = set()
    for query_cfg in cfg.get("queries", []):
        if isinstance(query_cfg, dict):
            query = query_cfg.get("query")
        else:
            query = str(query_cfg)
        if not query:
            continue
        key = query.strip().lower()
        if key in seen_terms:
            continue
        seen_terms.add(key)
        search_terms.append({"query": query, "kind": "role"})

    for company in _load_company_watchlist(cfg):
        key = company.strip().lower()
        if key in seen_terms:
            continue
        seen_terms.add(key)
        search_terms.append({"query": company, "kind": "company"})

    if not search_terms or not locations:
        log.warning("HiringCafe skipped: no search terms or locations configured")
        return {"new": 0, "existing": 0, "errors": 0, "queries": 0}

    results_per_site = int(hiring_cfg.get("results_per_site") or cfg.get("defaults", {}).get("results_per_site", 50))
    max_pages = int(hiring_cfg.get("max_pages", DEFAULT_MAX_PAGES))
    company_results_per_site = int(
        hiring_cfg.get("company_results_per_site") or min(results_per_site, DEFAULT_COMPANY_RESULTS_PER_SITE)
    )
    company_max_pages = int(hiring_cfg.get("company_max_pages", DEFAULT_COMPANY_MAX_PAGES))
    request_delay_seconds = _float_config(
        hiring_cfg.get("request_delay_seconds"),
        DEFAULT_REQUEST_DELAY_SECONDS,
    )
    company_request_delay_seconds = _float_config(
        hiring_cfg.get("company_request_delay_seconds"),
        max(request_delay_seconds, DEFAULT_COMPANY_REQUEST_DELAY_SECONDS),
    )
    rate_limit_backoff_seconds = _float_config(
        hiring_cfg.get("rate_limit_backoff_seconds"),
        DEFAULT_RATE_LIMIT_BACKOFF_SECONDS,
    )
    max_consecutive_rate_limits = _int_config(
        hiring_cfg.get("max_consecutive_rate_limits"),
        DEFAULT_MAX_CONSECUTIVE_RATE_LIMITS,
    )
    accept_locs, reject_locs = _load_location_config(cfg)

    init_db()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html"})

    total_new = 0
    total_existing = 0
    total_filtered = 0
    total_seen = 0
    errors = 0
    completed = 0
    rate_limit_hits = 0
    consecutive_rate_limits = 0
    stopped_for_rate_limit = False

    log.info(
        "HiringCafe crawl: %d search combinations (%d role, %d company) | "
        "Results/search: role %d, company %d | Max pages: role %d, company %d | "
        "Delay: role %.2fs, company %.2fs | 429 backoff %.1fs, stop after %d consecutive",
        len(search_terms) * len(locations),
        sum(1 for term in search_terms if term["kind"] == "role"),
        sum(1 for term in search_terms if term["kind"] == "company"),
        results_per_site,
        company_results_per_site,
        max_pages,
        company_max_pages,
        request_delay_seconds,
        company_request_delay_seconds,
        rate_limit_backoff_seconds,
        max_consecutive_rate_limits,
    )

    for term in search_terms:
        query = term["query"]
        search_results_per_site = company_results_per_site if term["kind"] == "company" else results_per_site
        search_max_pages = company_max_pages if term["kind"] == "company" else max_pages
        for loc_cfg in locations:
            location = loc_cfg.get("location") or cfg.get("defaults", {}).get("location", "United States")
            result = {"rate_limited": False}
            try:
                result = _run_one_search(
                    session,
                    query,
                    location,
                    search_results_per_site,
                    search_max_pages,
                    accept_locs,
                    reject_locs,
                )
                total_new += result["new"]
                total_existing += result["existing"]
                total_filtered += result["filtered"]
                total_seen += result["seen"]
                errors += int(result.get("errors", 0) or 0)
                if result.get("rate_limited"):
                    rate_limit_hits += 1
                    consecutive_rate_limits += 1
                    if max_consecutive_rate_limits and consecutive_rate_limits >= max_consecutive_rate_limits:
                        stopped_for_rate_limit = True
                        log.warning(
                            "HiringCafe stopping early after %d consecutive HTTP 429 responses; "
                            "rerun later to resume from remaining configured searches.",
                            consecutive_rate_limits,
                        )
                    else:
                        retry_after = result.get("retry_after_seconds")
                        backoff = max(
                            rate_limit_backoff_seconds,
                            float(retry_after) if retry_after is not None else 0.0,
                        )
                        if backoff > 0:
                            log.warning("HiringCafe HTTP 429 backoff: waiting %.1fs before next request", backoff)
                            time.sleep(backoff)
                else:
                    consecutive_rate_limits = 0
            except Exception as e:
                errors += 1
                log.exception("[HiringCafe] \"%s\" in %s failed: %s", query, location, e)
            completed += 1
            if stopped_for_rate_limit:
                break
            delay = company_request_delay_seconds if term["kind"] == "company" else request_delay_seconds
            if delay > 0 and not result.get("rate_limited"):
                time.sleep(delay)
        if stopped_for_rate_limit:
            break

    log.info(
        "HiringCafe complete: %d new | %d dupes | %d filtered | %d seen | %d errors | %d rate limits",
        total_new,
        total_existing,
        total_filtered,
        total_seen,
        errors,
        rate_limit_hits,
    )

    return {
        "new": total_new,
        "existing": total_existing,
        "filtered": total_filtered,
        "seen": total_seen,
        "errors": errors,
        "rate_limited": rate_limit_hits > 0,
        "stopped_for_rate_limit": stopped_for_rate_limit,
        "rate_limit_hits": rate_limit_hits,
        "queries": completed,
    }
