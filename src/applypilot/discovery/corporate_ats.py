"""Corporate ATS discovery for Greenhouse, Lever, and Ashby.

These ATS platforms expose public job-board JSON APIs. This module turns a
company watchlist into likely board tokens, fetches public postings, filters
them by the user's configured roles and locations, and stores matches directly
in the ApplyPilot database.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests
import yaml
from bs4 import BeautifulSoup

from applypilot import config
from applypilot.database import get_connection, init_db
from applypilot.discovery.jobspy import _load_location_config, _location_ok

log = logging.getLogger(__name__)
_CACHE_LOCK = threading.Lock()

DEFAULT_SOURCES = ["greenhouse", "lever", "ashby"]
DEFAULT_TIMEOUT = 12
DEFAULT_WORKERS = 6
DEFAULT_RESULTS_PER_COMPANY = 80
DEFAULT_TOKEN_CANDIDATES = 4
DEFAULT_CACHE_TTL_DAYS = 14
DEFAULT_REQUEST_RETRIES = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 1.0
DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 20.0
NOT_FOUND_STATUS_CODES = {404, 410}
RETRY_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

QUERY_ALIASES = {
    "chief of staff": [
        "founder associate",
        "founder's associate",
        "business operations",
        "bizops",
        "strategy and operations",
        "strategy & operations",
    ],
    "coo": [
        "chief operating officer",
        "head of operations",
        "operations lead",
        "business operations",
    ],
    "strategy operations": [
        "strategy & operations",
        "strategy and operations",
        "business operations",
        "bizops",
        "biz ops",
    ],
    "business development": [
        "partnerships",
        "strategic partnerships",
        "growth partnerships",
        "gtm",
    ],
    "sales engineer": [
        "solutions engineer",
        "solution engineer",
        "pre-sales",
        "presales",
    ],
}


class CorporateAtsRequestError(Exception):
    """Network or HTTP failure while probing a public ATS endpoint."""

    def __init__(self, message: str, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def _truthy_config(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _clean_company_name(raw: str) -> str | None:
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


def _load_company_watchlist(search_cfg: dict, ats_cfg: dict) -> list[str]:
    hiring_cfg = search_cfg.get("hiring_cafe", {}) or {}
    path_value = (
        ats_cfg.get("company_watchlist_path")
        or search_cfg.get("company_watchlist_path")
        or hiring_cfg.get("company_watchlist_path")
    )
    if not path_value:
        log.warning("Corporate ATS skipped: no company watchlist path configured")
        return []

    path = Path(path_value).expanduser()
    if not path.exists():
        log.warning("Corporate ATS company watchlist not found: %s", path)
        return []

    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError as e:
        log.warning("Corporate ATS company watchlist could not be read: %s", e)
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
        limit = int(ats_cfg.get("company_watchlist_limit", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit > 0:
        companies = companies[:limit]

    log.info("Corporate ATS company watchlist: %d companies from %s", len(companies), path)
    return companies


def _load_exact_boards(ats_cfg: dict) -> dict:
    path_value = ats_cfg.get("exact_boards_path")
    path = Path(path_value).expanduser() if path_value else config.APP_DIR / "corporate_ats.yaml"
    if not path.exists():
        return {}

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("Corporate ATS exact board config could not be read: %s", e)
        return {}

    entries = data.get("companies") or data.get("boards") or {}
    normalized: dict[str, dict] = {}
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or key
        normalized[_company_key(str(name))] = entry
        normalized[_company_key(str(key))] = entry
    return normalized


def _token_candidates(company: str, limit: int) -> list[str]:
    normalized = company.lower()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"['`]", "", normalized)
    words = re.findall(r"[a-z0-9]+", normalized)
    suffixes = {
        "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
        "company", "co", "technologies", "technology", "labs", "ai", "io", "com",
    }

    word_sets = [words]
    if len(words) > 1 and words[-1] in suffixes:
        word_sets.append(words[:-1])
    if len(words) > 2 and words[-2] in {"technologies", "technology", "labs"}:
        word_sets.append(words[:-2] + [words[-1]])

    candidates: list[str] = []
    seen: set[str] = set()
    for word_set in word_sets:
        if not word_set:
            continue
        variants = [
            "".join(word_set),
            "-".join(word_set),
            "_".join(word_set),
        ]
        if len(word_set) > 1:
            variants.append(word_set[0])
        for variant in variants:
            token = variant.strip("-_")
            if token and token not in seen:
                seen.add(token)
                candidates.append(token)
            if limit > 0 and len(candidates) >= limit:
                return candidates

    return candidates


def _html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    text = BeautifulSoup(html.unescape(value), "html.parser").get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or None


def _metadata_salary(metadata) -> str | None:
    if not metadata:
        return None
    items = metadata if isinstance(metadata, list) else [metadata]
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("name") or item.get("label") or item.get("title") or "").lower()
        value = item.get("value") or item.get("text")
        if value and any(word in label for word in ("salary", "compensation", "pay", "range")):
            return str(value)
    return None


def _ashby_salary(job: dict) -> str | None:
    compensation = job.get("compensation")
    if not compensation:
        return None
    if isinstance(compensation, str):
        return compensation
    if isinstance(compensation, dict):
        for key in ("compensationTierSummary", "summary", "displayValue", "payRange"):
            if compensation.get(key):
                return str(compensation[key])
        parts = []
        for key in ("min", "max", "currencyCode", "interval"):
            if compensation.get(key):
                parts.append(str(compensation[key]))
        return " ".join(parts) if parts else None
    return str(compensation)


def _job_matches_queries(job: dict, queries: list[str]) -> bool:
    if not queries:
        return True
    text = " ".join(
        str(job.get(field) or "")
        for field in ("title", "description", "full_description", "department", "team")
    ).lower()
    for query in queries:
        q = query.lower().strip()
        if not q:
            continue
        variants = [q, *QUERY_ALIASES.get(q, [])]
        for variant in variants:
            if variant in text:
                return True
            words = [w for w in re.findall(r"[a-z0-9]+", variant) if len(w) > 2]
            if len(words) > 1 and all(word in text for word in words):
                return True
    return False


def _queries_from_config(search_cfg: dict, ats_cfg: dict) -> list[str]:
    if _truthy_config(ats_cfg.get("match_queries", True)) is False:
        return []
    queries: list[str] = []
    for item in search_cfg.get("queries", []):
        query = item.get("query") if isinstance(item, dict) else str(item)
        if query:
            queries.append(query)
    return queries


def _request_options(ats_cfg: dict) -> dict:
    timeout = float(ats_cfg.get("request_timeout", DEFAULT_TIMEOUT))
    return {
        "connect_timeout": float(ats_cfg.get("connect_timeout", min(timeout, 5.0))),
        "read_timeout": float(ats_cfg.get("read_timeout", timeout)),
        "retries": int(ats_cfg.get("request_retries", DEFAULT_REQUEST_RETRIES)),
        "backoff": float(ats_cfg.get("retry_backoff_seconds", DEFAULT_RETRY_BACKOFF_SECONDS)),
        "rate_limit_backoff": float(
            ats_cfg.get("rate_limit_backoff_seconds", DEFAULT_RATE_LIMIT_BACKOFF_SECONDS)
        ),
    }


def _retry_after_seconds(resp: requests.Response, default_wait: float) -> float:
    retry_after = resp.headers.get("Retry-After")
    if not retry_after:
        return default_wait
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(retry_after)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return default_wait


def _request_json(url: str, request_options: dict) -> tuple[int, dict | list | None]:
    attempts = max(1, int(request_options.get("retries", DEFAULT_REQUEST_RETRIES)) + 1)
    timeout = (
        float(request_options.get("connect_timeout", 5.0)),
        float(request_options.get("read_timeout", DEFAULT_TIMEOUT)),
    )
    backoff = float(request_options.get("backoff", DEFAULT_RETRY_BACKOFF_SECONDS))
    rate_limit_backoff = float(
        request_options.get("rate_limit_backoff", DEFAULT_RATE_LIMIT_BACKOFF_SECONDS)
    )

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            last_error = e
            if attempt < attempts:
                time.sleep(backoff * attempt)
                continue
            raise CorporateAtsRequestError(str(e), retryable=True) from e

        status = resp.status_code
        if status in NOT_FOUND_STATUS_CODES:
            return status, None
        if status in RETRY_STATUS_CODES and attempt < attempts:
            wait = rate_limit_backoff if status == 429 else backoff * attempt
            wait = min(_retry_after_seconds(resp, wait), rate_limit_backoff if status == 429 else wait)
            if wait > 0:
                time.sleep(wait)
            continue
        if status >= 400:
            message = resp.text[:240].replace("\n", " ").strip()
            raise CorporateAtsRequestError(
                f"HTTP {status}: {message}",
                status_code=status,
                retryable=status in RETRY_STATUS_CODES,
            )
        try:
            return status, resp.json()
        except ValueError as e:
            raise CorporateAtsRequestError(
                f"Invalid JSON response from {url}",
                status_code=status,
                retryable=False,
            ) from e

    raise CorporateAtsRequestError(str(last_error or "request failed"), retryable=True)


def _fetch_greenhouse(
    company: str,
    token: str,
    request_options: dict,
    results_limit: int,
) -> tuple[bool, list[dict]]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{quote(token)}/jobs?content=true"
    status, data = _request_json(url, request_options)
    if status in NOT_FOUND_STATUS_CODES or not isinstance(data, dict):
        return False, []

    jobs: list[dict] = []
    for item in data.get("jobs", [])[:results_limit]:
        if not isinstance(item, dict):
            continue
        content = _html_to_text(item.get("content"))
        location = None
        if isinstance(item.get("location"), dict):
            location = item["location"].get("name")
        offices = item.get("offices") or []
        office_locs = [
            office.get("location") or office.get("name")
            for office in offices
            if isinstance(office, dict) and (office.get("location") or office.get("name"))
        ]
        if office_locs:
            location = ", ".join(dict.fromkeys([location, *office_locs] if location else office_locs))
        departments = item.get("departments") or []
        department = ", ".join(
            d.get("name") for d in departments if isinstance(d, dict) and d.get("name")
        )
        jobs.append({
            "url": item.get("absolute_url") or f"https://boards.greenhouse.io/{token}/jobs/{item.get('id')}",
            "title": item.get("title"),
            "salary": _metadata_salary(item.get("metadata")),
            "description": content[:500] if content else None,
            "full_description": content,
            "location": location,
            "application_url": item.get("absolute_url"),
            "company": company,
            "department": department,
            "strategy": "greenhouse_api",
        })
    return True, jobs


def _fetch_lever(
    company: str,
    token: str,
    request_options: dict,
    results_limit: int,
    eu: bool = False,
) -> tuple[bool, list[dict]]:
    host = "api.eu.lever.co" if eu else "api.lever.co"
    url = f"https://{host}/v0/postings/{quote(token)}?mode=json"
    status, data = _request_json(url, request_options)
    if status in NOT_FOUND_STATUS_CODES or not isinstance(data, list):
        return False, []

    jobs: list[dict] = []
    for item in data[:results_limit]:
        if not isinstance(item, dict):
            continue
        categories = item.get("categories") or {}
        lists = item.get("lists") or []
        list_text = "\n\n".join(
            f"{section.get('text', '')}\n{_html_to_text(section.get('content')) or ''}".strip()
            for section in lists
            if isinstance(section, dict)
        )
        description = item.get("descriptionPlain") or _html_to_text(item.get("description"))
        additional = item.get("additionalPlain") or _html_to_text(item.get("additional"))
        full_description = "\n\n".join(part for part in (description, list_text, additional) if part)
        locations = categories.get("allLocations") or categories.get("location")
        if isinstance(locations, list):
            location = ", ".join(str(loc) for loc in locations if loc)
        else:
            location = str(locations) if locations else None
        jobs.append({
            "url": item.get("hostedUrl") or item.get("applyUrl") or f"https://jobs.lever.co/{token}/{item.get('id')}",
            "title": item.get("text"),
            "salary": None,
            "description": full_description[:500] if full_description else None,
            "full_description": full_description or None,
            "location": location,
            "application_url": item.get("applyUrl") or item.get("hostedUrl"),
            "company": company,
            "department": categories.get("department") or categories.get("team"),
            "team": categories.get("team"),
            "strategy": "lever_api_eu" if eu else "lever_api",
        })
    return True, jobs


def _fetch_ashby(
    company: str,
    token: str,
    request_options: dict,
    results_limit: int,
) -> tuple[bool, list[dict]]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{quote(token)}?includeCompensation=true"
    status, data = _request_json(url, request_options)
    if status in NOT_FOUND_STATUS_CODES or not isinstance(data, dict):
        return False, []

    jobs: list[dict] = []
    for item in data.get("jobs", [])[:results_limit]:
        if not isinstance(item, dict) or item.get("isListed") is False:
            continue
        full_description = item.get("descriptionPlain") or _html_to_text(item.get("descriptionHtml"))
        secondary = item.get("secondaryLocations") or []
        locations = [item.get("location")]
        locations.extend(
            loc.get("location") for loc in secondary if isinstance(loc, dict) and loc.get("location")
        )
        location = ", ".join(dict.fromkeys(str(loc) for loc in locations if loc))
        job_id = item.get("id") or item.get("jobId")
        public_url = (
            item.get("jobUrl")
            or item.get("applicationUrl")
            or item.get("applyUrl")
            or f"https://jobs.ashbyhq.com/{quote(token)}/{job_id}"
        )
        jobs.append({
            "url": public_url,
            "title": item.get("title"),
            "salary": _ashby_salary(item),
            "description": full_description[:500] if full_description else None,
            "full_description": full_description,
            "location": location or None,
            "application_url": item.get("applicationUrl") or item.get("applyUrl") or public_url,
            "company": company,
            "department": item.get("department"),
            "team": item.get("team"),
            "strategy": "ashby_api",
        })
    return True, jobs


def _load_cache(ats_cfg: dict) -> dict:
    if not _truthy_config(ats_cfg.get("cache_enabled", True)):
        return {"entries": {}}
    path_value = ats_cfg.get("cache_path")
    path = Path(path_value).expanduser() if path_value else config.APP_DIR / "corporate_ats_cache.json"
    if not path.exists():
        return {"entries": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"entries": {}}


def _save_cache(cache: dict, ats_cfg: dict) -> None:
    if not _truthy_config(ats_cfg.get("cache_enabled", True)):
        return
    path_value = ats_cfg.get("cache_path")
    path = Path(path_value).expanduser() if path_value else config.APP_DIR / "corporate_ats_cache.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(path)
    except OSError as e:
        log.warning("Could not write Corporate ATS cache: %s", e)


def _cache_key(source: str, token: str) -> str:
    return f"{source}:{token}"


def _is_negative_cached(cache: dict, source: str, token: str, ttl_days: int) -> bool:
    with _CACHE_LOCK:
        entry = (cache.get("entries") or {}).get(_cache_key(source, token))
    if not entry or entry.get("ok") is not False:
        return False
    ts = float(entry.get("ts") or 0)
    return time.time() - ts < ttl_days * 86400


def _positive_cached_tokens(cache: dict, source: str, company: str, ttl_days: int) -> list[str]:
    company_key = _company_key(company)
    now = time.time()
    tokens: list[str] = []
    with _CACHE_LOCK:
        entries = dict(cache.get("entries") or {})
    for key, entry in entries.items():
        if not key.startswith(f"{source}:"):
            continue
        if entry.get("ok") is not True:
            continue
        if _company_key(str(entry.get("company") or "")) != company_key:
            continue
        ts = float(entry.get("ts") or 0)
        if now - ts >= ttl_days * 86400:
            continue
        tokens.append(key.split(":", 1)[1])
    return tokens


def _cache_result(cache: dict, source: str, token: str, ok: bool, company: str) -> None:
    with _CACHE_LOCK:
        cache.setdefault("entries", {})[_cache_key(source, token)] = {
            "ok": ok,
            "company": company,
            "ts": time.time(),
        }


def _source_tokens(company: str, source: str, exact_boards: dict, candidate_limit: int) -> list[str]:
    entry = exact_boards.get(_company_key(company), {})
    tokens: list[str] = []
    exact_value = entry.get(source) if isinstance(entry, dict) else None
    if isinstance(exact_value, list):
        tokens.extend(str(value) for value in exact_value if value)
    elif exact_value:
        tokens.append(str(exact_value))

    for token in _token_candidates(company, candidate_limit):
        if token not in tokens:
            tokens.append(token)
    return tokens


def _prioritized_source_tokens(
    company: str,
    source: str,
    cache_source: str,
    exact_boards: dict,
    candidate_limit: int,
    cache: dict,
    ttl_days: int,
) -> list[str]:
    tokens: list[str] = []
    for token in _positive_cached_tokens(cache, cache_source, company, ttl_days):
        if token not in tokens:
            tokens.append(token)
    for token in _source_tokens(company, source, exact_boards, candidate_limit):
        if token not in tokens:
            tokens.append(token)
    return tokens


def _fetch_source(
    source: str,
    company: str,
    token: str,
    request_options: dict,
    results_limit: int,
) -> tuple[bool, list[dict]]:
    if source == "greenhouse":
        return _fetch_greenhouse(company, token, request_options, results_limit)
    if source == "lever":
        return _fetch_lever(company, token, request_options, results_limit, eu=False)
    if source == "lever_eu":
        return _fetch_lever(company, token, request_options, results_limit, eu=True)
    if source == "ashby":
        return _fetch_ashby(company, token, request_options, results_limit)
    raise ValueError(f"Unsupported corporate ATS source: {source}")


def _process_company(
    company: str,
    sources: list[str],
    exact_boards: dict,
    ats_cfg: dict,
    queries: list[str],
    accept_locs: list[str],
    reject_locs: list[str],
    cache: dict,
) -> dict:
    request_options = _request_options(ats_cfg)
    results_limit = int(ats_cfg.get("results_per_company", DEFAULT_RESULTS_PER_COMPANY))
    candidate_limit = int(ats_cfg.get("token_candidates_per_company", DEFAULT_TOKEN_CANDIDATES))
    ttl_days = int(ats_cfg.get("cache_ttl_days", DEFAULT_CACHE_TTL_DAYS))

    jobs: list[dict] = []
    checked = 0
    matched_boards = 0
    errors = 0
    filtered = 0
    seen = 0
    cached_skips = 0
    error_types: dict[str, int] = {}

    def record_error(kind: str) -> None:
        error_types[kind] = error_types.get(kind, 0) + 1

    expanded_sources: list[str] = []
    for source in sources:
        expanded_sources.append(source)
        if source == "lever" and _truthy_config(ats_cfg.get("try_lever_eu", True)):
            expanded_sources.append("lever_eu")

    for source in expanded_sources:
        cache_source = source
        token_source = source.replace("_eu", "")
        for token in _prioritized_source_tokens(
            company, token_source, cache_source, exact_boards, candidate_limit,
            cache, ttl_days,
        ):
            if _is_negative_cached(cache, cache_source, token, ttl_days):
                cached_skips += 1
                continue
            checked += 1
            try:
                valid, found = _fetch_source(source, company, token, request_options, results_limit)
            except CorporateAtsRequestError as e:
                if e.status_code in NOT_FOUND_STATUS_CODES:
                    _cache_result(cache, cache_source, token, False, company)
                    continue
                errors += 1
                kind = f"http_{e.status_code}" if e.status_code else "network"
                if e.retryable:
                    kind += "_retryable"
                record_error(kind)
                log.warning("[Corporate ATS] %s %s/%s request error: %s", company, source, token, e)
                continue
            except Exception as e:
                errors += 1
                record_error(type(e).__name__)
                log.warning("[Corporate ATS] %s %s/%s error: %s", company, source, token, e)
                continue

            _cache_result(cache, cache_source, token, valid, company)
            if not valid:
                continue

            matched_boards += 1
            seen += len(found)
            for job in found:
                if not _location_ok(job.get("location"), accept_locs, reject_locs):
                    filtered += 1
                    continue
                if not _job_matches_queries(job, queries):
                    filtered += 1
                    continue
                jobs.append(job)
            break

    return {
        "company": company,
        "jobs": jobs,
        "checked": checked,
        "matched_boards": matched_boards,
        "seen": seen,
        "filtered": filtered,
        "errors": errors,
        "cached_skips": cached_skips,
        "error_types": error_types,
    }


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
                    job.get("company") or "Corporate",
                    job.get("strategy") or "corporate_ats_api",
                    now,
                    job.get("company") or "Corporate",
                    "corporate_ats",
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


def _merge_error_types(total: dict[str, int], result: dict) -> None:
    for key, count in (result.get("error_types") or {}).items():
        total[key] = total.get(key, 0) + int(count)


def _company_report(result: dict) -> dict:
    return {
        "company": result.get("company"),
        "checked": result.get("checked", 0),
        "cached_skips": result.get("cached_skips", 0),
        "matched_boards": result.get("matched_boards", 0),
        "seen": result.get("seen", 0),
        "kept": len(result.get("jobs") or []),
        "filtered": result.get("filtered", 0),
        "errors": result.get("errors", 0),
        "error_types": result.get("error_types") or {},
    }


def _write_run_report(report: dict, ats_cfg: dict) -> str | None:
    if not _truthy_config(ats_cfg.get("write_run_report", True)):
        return None
    path_value = ats_cfg.get("run_report_path")
    path = Path(path_value).expanduser() if path_value else config.APP_DIR / "corporate_ats_last_run.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(path)
        return str(path)
    except OSError as e:
        log.warning("Could not write Corporate ATS run report: %s", e)
        return None


def run_corporate_ats_discovery(cfg: dict | None = None, workers: int | None = None) -> dict:
    """Run corporate ATS discovery across Greenhouse, Lever, and Ashby."""
    if cfg is None:
        cfg = config.load_search_config()

    ats_cfg = cfg.get("corporate_ats", {}) or {}
    if not _truthy_config(ats_cfg.get("enabled", False)):
        log.info("Corporate ATS discovery disabled in search config")
        return {"new": 0, "existing": 0, "errors": 0, "companies": 0, "matched_boards": 0}

    sources = ats_cfg.get("sources") or DEFAULT_SOURCES
    sources = [str(source).lower() for source in sources if str(source).lower() in DEFAULT_SOURCES]
    companies = _load_company_watchlist(cfg, ats_cfg)
    if not companies or not sources:
        return {"new": 0, "existing": 0, "errors": 0, "companies": 0, "matched_boards": 0}

    exact_boards = _load_exact_boards(ats_cfg)
    queries = _queries_from_config(cfg, ats_cfg)
    accept_locs, reject_locs = _load_location_config(cfg)
    cache = _load_cache(ats_cfg)
    worker_count = workers or int(ats_cfg.get("workers", DEFAULT_WORKERS))
    worker_count = max(1, min(worker_count, len(companies)))

    init_db()
    log.info(
        "Corporate ATS crawl: %d companies | sources=%s | workers=%d | role filter=%s",
        len(companies),
        ", ".join(sources),
        worker_count,
        "on" if queries else "off",
    )

    all_jobs: list[dict] = []
    total_checked = 0
    total_matched = 0
    total_seen = 0
    total_filtered = 0
    total_errors = 0
    total_cached_skips = 0
    error_types: dict[str, int] = {}
    company_reports: list[dict] = []

    if worker_count > 1 and len(companies) > 1:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {
                pool.submit(
                    _process_company,
                    company,
                    sources,
                    exact_boards,
                    ats_cfg,
                    queries,
                    accept_locs,
                    reject_locs,
                    cache,
                ): company
                for company in companies
            }
            for i, future in enumerate(as_completed(futures), 1):
                result = future.result()
                all_jobs.extend(result["jobs"])
                total_checked += result["checked"]
                total_matched += result["matched_boards"]
                total_seen += result["seen"]
                total_filtered += result["filtered"]
                total_errors += result["errors"]
                total_cached_skips += result.get("cached_skips", 0)
                _merge_error_types(error_types, result)
                company_reports.append(_company_report(result))
                if i % 25 == 0 or i == len(futures):
                    log.info(
                        "Corporate ATS progress: %d/%d companies | %d boards matched | %d jobs kept",
                        i,
                        len(futures),
                        total_matched,
                        len(all_jobs),
                    )
    else:
        for i, company in enumerate(companies, 1):
            result = _process_company(
                company, sources, exact_boards, ats_cfg, queries,
                accept_locs, reject_locs, cache,
            )
            all_jobs.extend(result["jobs"])
            total_checked += result["checked"]
            total_matched += result["matched_boards"]
            total_seen += result["seen"]
            total_filtered += result["filtered"]
            total_errors += result["errors"]
            total_cached_skips += result.get("cached_skips", 0)
            _merge_error_types(error_types, result)
            company_reports.append(_company_report(result))
            if i % 25 == 0 or i == len(companies):
                log.info(
                    "Corporate ATS progress: %d/%d companies | %d boards matched | %d jobs kept",
                    i,
                    len(companies),
                    total_matched,
                    len(all_jobs),
                )

    conn = get_connection()
    new, existing = _store_jobs(conn, all_jobs)
    _save_cache(cache, ats_cfg)
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "companies": len(companies),
        "sources": sources,
        "workers": worker_count,
        "queries": queries,
        "summary": {
            "new": new,
            "existing": existing,
            "kept": len(all_jobs),
            "filtered": total_filtered,
            "seen": total_seen,
            "errors": total_errors,
            "error_types": error_types,
            "matched_boards": total_matched,
            "board_checks": total_checked,
            "cached_skips": total_cached_skips,
        },
        "company_results": company_reports,
    }
    report_path = _write_run_report(report, ats_cfg)

    log.info(
        "Corporate ATS complete: %d new | %d dupes | %d kept | %d filtered | "
        "%d seen | %d boards matched | %d board checks | %d cached skips | %d errors",
        new,
        existing,
        len(all_jobs),
        total_filtered,
        total_seen,
        total_matched,
        total_checked,
        total_cached_skips,
        total_errors,
    )
    if report_path:
        log.info("Corporate ATS report: %s", report_path)

    return {
        "new": new,
        "existing": existing,
        "kept": len(all_jobs),
        "filtered": total_filtered,
        "seen": total_seen,
        "errors": total_errors,
        "companies": len(companies),
        "matched_boards": total_matched,
        "board_checks": total_checked,
        "cached_skips": total_cached_skips,
        "error_types": error_types,
        "report_path": report_path,
    }
