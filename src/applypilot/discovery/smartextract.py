"""AI-powered smart extraction: discovers jobs from arbitrary websites.

Two-phase approach:
  Phase 1: Lightweight intelligence (JSON-LD, API responses, data-testids, DOM stats)
           -> LLM picks the best extraction strategy
  Phase 2: Only for CSS selectors -- Playwright finds repeating card elements,
           extracts 2-3 examples, sends focused HTML to LLM for selector generation.

JSON-LD and API strategies execute directly from stored data -- no LLM needed.

Sites are loaded from config/sites.yaml, with {query_encoded} and {location_encoded}
placeholders replaced from the user's search configuration.
"""

import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from applypilot import config
from applypilot.config import CONFIG_DIR
from applypilot.database import init_db, get_stats, insert_discovered_job
from applypilot.llm import get_client

log = logging.getLogger(__name__)

# Fix Windows encoding -- prevents charmap errors on emoji/unicode in job titles
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _cfg_bool(cfg: dict, key: str, default: bool) -> bool:
    value = cfg.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


SMART_GOTO_TIMEOUT_MS = _env_int("APPLYPILOT_SMART_GOTO_TIMEOUT_MS", 30000)
SMART_ACTION_TIMEOUT_MS = _env_int("APPLYPILOT_SMART_ACTION_TIMEOUT_MS", 8000)
SMART_NETWORK_IDLE_TIMEOUT_MS = _env_int("APPLYPILOT_SMART_NETWORK_IDLE_TIMEOUT_MS", 5000)
SMART_WAIT_FOR_NETWORK_IDLE = _env_bool("APPLYPILOT_SMART_WAIT_FOR_NETWORK_IDLE", False)
SMART_HEADFUL_RETRY = _env_bool("APPLYPILOT_SMART_HEADFUL_RETRY", False)
SMART_BLOCK_RESOURCES = _env_bool("APPLYPILOT_SMART_BLOCK_RESOURCES", True)
SMART_MAX_RESPONSE_BYTES = _env_int("APPLYPILOT_SMART_MAX_RESPONSE_BYTES", 2_000_000)
SMART_HTTP_PROBE_ENABLED = _env_bool("APPLYPILOT_SMART_HTTP_PROBE_ENABLED", True)
SMART_HEALTH_ENABLED = _env_bool("APPLYPILOT_SMART_HEALTH_ENABLED", True)
SMART_SKIP_AFTER_FAILURES = _env_int("APPLYPILOT_SMART_SKIP_AFTER_FAILURES", 3)
SMART_SKIP_COOLDOWN_HOURS = _env_int("APPLYPILOT_SMART_SKIP_COOLDOWN_HOURS", 24)
SMART_MIN_VALID_JOBS_FOR_SUCCESS = _env_int("APPLYPILOT_SMART_MIN_VALID_JOBS_FOR_SUCCESS", 1)
SMART_HEALTH_PATH = config.APP_DIR / "smartextract_health.json"


def _apply_smart_config(smart_cfg: dict) -> None:
    """Apply per-run Smart Extract settings from searches.yaml."""
    global SMART_GOTO_TIMEOUT_MS
    global SMART_ACTION_TIMEOUT_MS
    global SMART_NETWORK_IDLE_TIMEOUT_MS
    global SMART_WAIT_FOR_NETWORK_IDLE
    global SMART_HEADFUL_RETRY
    global SMART_BLOCK_RESOURCES
    global SMART_MAX_RESPONSE_BYTES
    global SMART_HTTP_PROBE_ENABLED
    global SMART_HEALTH_ENABLED
    global SMART_SKIP_AFTER_FAILURES
    global SMART_SKIP_COOLDOWN_HOURS
    global SMART_MIN_VALID_JOBS_FOR_SUCCESS

    SMART_GOTO_TIMEOUT_MS = _cfg_int(smart_cfg, "goto_timeout_ms", SMART_GOTO_TIMEOUT_MS)
    SMART_ACTION_TIMEOUT_MS = _cfg_int(smart_cfg, "action_timeout_ms", SMART_ACTION_TIMEOUT_MS)
    SMART_NETWORK_IDLE_TIMEOUT_MS = _cfg_int(
        smart_cfg, "network_idle_timeout_ms", SMART_NETWORK_IDLE_TIMEOUT_MS
    )
    SMART_WAIT_FOR_NETWORK_IDLE = _cfg_bool(
        smart_cfg, "wait_for_network_idle", SMART_WAIT_FOR_NETWORK_IDLE
    )
    SMART_HEADFUL_RETRY = _cfg_bool(smart_cfg, "headful_retry", SMART_HEADFUL_RETRY)
    SMART_BLOCK_RESOURCES = _cfg_bool(smart_cfg, "block_resources", SMART_BLOCK_RESOURCES)
    SMART_MAX_RESPONSE_BYTES = _cfg_int(
        smart_cfg, "max_response_bytes", SMART_MAX_RESPONSE_BYTES
    )
    SMART_HTTP_PROBE_ENABLED = _cfg_bool(
        smart_cfg, "http_probe_enabled", SMART_HTTP_PROBE_ENABLED
    )
    SMART_HEALTH_ENABLED = _cfg_bool(smart_cfg, "health_enabled", SMART_HEALTH_ENABLED)
    SMART_SKIP_AFTER_FAILURES = _cfg_int(
        smart_cfg, "skip_after_failures", SMART_SKIP_AFTER_FAILURES
    )
    SMART_SKIP_COOLDOWN_HOURS = _cfg_int(
        smart_cfg, "skip_cooldown_hours", SMART_SKIP_COOLDOWN_HOURS
    )
    SMART_MIN_VALID_JOBS_FOR_SUCCESS = _cfg_int(
        smart_cfg, "min_valid_jobs_for_success", SMART_MIN_VALID_JOBS_FOR_SUCCESS
    )


# -- Source health and validation --------------------------------------------

_BAD_TITLE_EXACT = {
    "apply",
    "apply now",
    "view job",
    "view jobs",
    "view all jobs",
    "search",
    "next",
    "previous",
    "sign in",
    "login",
    "learn more",
    "read more",
    "open roles",
    "see jobs",
}


def _is_http_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(str(value).strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _is_valid_smart_title(title: object) -> bool:
    cleaned = _clean_text(title)
    if len(cleaned) < 3:
        return False
    normalized = cleaned.lower()
    if normalized in _BAD_TITLE_EXACT:
        return False
    if len(cleaned.split()) <= 2 and normalized in {"jobs", "careers", "openings"}:
        return False
    return True


def _coerce_db_scalar(value):
    """Coerce a value to something SQLite can store (str/int/float/None).

    JSON-LD fields such as ``baseSalary`` are frequently structured objects or
    lists. Storing a dict/list directly raises ``sqlite3.InterfaceError`` and,
    because the insert is not individually guarded, aborts the entire discovery
    run. Stringify containers (and any other non-scalar) so one odd posting
    can't take down the batch.
    """
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)[:500]
        except Exception:
            return str(value)[:500]
    return str(value)[:500]


def validate_smart_jobs(jobs: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Reject malformed Smart Extract jobs before they enter the database."""
    counts: Counter[str] = Counter()
    valid: list[dict] = []
    seen_urls: set[str] = set()

    for job in jobs:
        title = _clean_text(job.get("title"))
        if not _is_valid_smart_title(title):
            counts["invalid_title"] += 1
            continue

        url = _clean_text(job.get("url"))
        if not _is_http_url(url):
            counts["invalid_url"] += 1
            continue

        normalized_url = url.rstrip("/")
        if normalized_url in seen_urls:
            counts["duplicate_url"] += 1
            continue
        seen_urls.add(normalized_url)

        normalized = dict(job)
        normalized["title"] = title
        normalized["url"] = url
        # Ensure DB-storable scalars: structured salary/location objects would
        # otherwise raise sqlite3.InterfaceError on insert. Only touch keys that
        # are present so we don't invent fields the caller didn't supply.
        if "salary" in normalized:
            normalized["salary"] = _coerce_db_scalar(normalized["salary"])
        if "location" in normalized:
            normalized["location"] = _coerce_db_scalar(normalized["location"])
        valid.append(normalized)
        counts["accepted"] += 1

    for key in ("accepted", "invalid_title", "invalid_url", "duplicate_url"):
        counts.setdefault(key, 0)
    return valid, dict(counts)


def make_skip_result(name: str, reason: str) -> dict:
    return {"name": name, "status": "SKIPPED", "skip_reason": reason, "jobs": []}


def detect_page_issue(html: str | None, url: str = "") -> dict | None:
    """Classify known anti-bot/challenge pages before spending browser or LLM time."""
    text = (html or "").lower()
    if not text:
        return None

    cloudflare_signals = (
        "just a moment...",
        "enable javascript and cookies to continue",
        "/cdn-cgi/challenge-platform/",
        "__cf_chl_",
        "cf_chl_",
        "cf-ray",
        "challenges.cloudflare.com",
    )
    if any(signal in text for signal in cloudflare_signals):
        return {
            "type": "cloudflare_challenge",
            "label": "Cloudflare challenge",
            "detail": "Cloudflare or JavaScript/cookie verification page detected",
        }

    captcha_signals = (
        "captcha",
        "are you a human",
        "verify you are human",
        "verify you",
        "please verify",
        "bot detection",
        "unusual requests",
        "access denied",
    )
    if any(signal in text for signal in captcha_signals):
        return {
            "type": "captcha_challenge",
            "label": "Captcha or bot challenge",
            "detail": "Captcha, bot verification, or access-denied page detected",
        }

    return None


def load_smart_health() -> dict:
    if not SMART_HEALTH_PATH.exists():
        return {}
    try:
        data = json.loads(SMART_HEALTH_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning("Could not read Smart Extract health file: %s", e)
        return {}


def save_smart_health(health: dict) -> None:
    try:
        SMART_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        SMART_HEALTH_PATH.write_text(json.dumps(health, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Could not write Smart Extract health file: %s", e)


def _parse_iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def should_skip_source(
    health: dict,
    name: str,
    *,
    skip_after_failures: int,
    cooldown_hours: int,
    now: datetime | None = None,
) -> tuple[bool, str]:
    if skip_after_failures <= 0 or cooldown_hours <= 0:
        return False, ""
    entry = health.get(name) or {}
    failures = int(entry.get("consecutive_failures") or 0)
    if failures < skip_after_failures:
        return False, ""
    last_checked = _parse_iso(entry.get("last_checked_at"))
    if not last_checked:
        return True, f"health cooldown: {failures} failures"
    elapsed = (now or datetime.now(timezone.utc)) - last_checked
    if elapsed.total_seconds() < cooldown_hours * 3600:
        return True, f"health cooldown: {failures} failures"
    return False, ""


def record_source_health(
    health: dict,
    name: str,
    *,
    url: str,
    status: str,
    strategy: str | None,
    jobs_found: int,
    elapsed_seconds: float,
    hard_failure: bool,
    timeout: bool = False,
    issue_type: str | None = None,
    challenge: bool = False,
) -> None:
    entry = dict(health.get(name) or {})
    runs = int(entry.get("runs") or 0) + 1
    previous_avg = float(entry.get("average_runtime_seconds") or 0.0)
    average_runtime = (
        elapsed_seconds if runs == 1 else ((previous_avg * (runs - 1)) + elapsed_seconds) / runs
    )

    entry.update({
        "source": name,
        "last_url": url,
        "last_status": status,
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
        "last_jobs_found": int(jobs_found or 0),
        "runs": runs,
        "average_runtime_seconds": round(average_runtime, 3),
    })
    entry["timeout_count"] = int(entry.get("timeout_count") or 0) + (1 if timeout else 0)
    entry["challenge_count"] = int(entry.get("challenge_count") or 0) + (1 if challenge else 0)
    if issue_type:
        entry["last_issue_type"] = issue_type
    elif not hard_failure:
        entry["last_issue_type"] = ""
    if hard_failure:
        entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
    else:
        entry["consecutive_failures"] = 0
        if strategy:
            entry["last_successful_strategy"] = strategy
    health[name] = entry


def summarize_smart_health(
    health: dict,
    *,
    now: datetime | None = None,
    skip_after_failures: int | None = None,
    cooldown_hours: int | None = None,
) -> list[dict]:
    """Return source-health rows suitable for CLI display."""
    now = now or datetime.now(timezone.utc)
    skip_after_failures = SMART_SKIP_AFTER_FAILURES if skip_after_failures is None else skip_after_failures
    cooldown_hours = SMART_SKIP_COOLDOWN_HOURS if cooldown_hours is None else cooldown_hours

    rows: list[dict] = []
    for name, entry in sorted((health or {}).items()):
        failures = int(entry.get("consecutive_failures") or 0)
        should_skip, reason = should_skip_source(
            health,
            name,
            skip_after_failures=skip_after_failures,
            cooldown_hours=cooldown_hours,
            now=now,
        )
        rows.append({
            "source": entry.get("source") or name,
            "status": entry.get("last_status") or "",
            "issue_type": entry.get("last_issue_type") or "",
            "failures": failures,
            "timeouts": int(entry.get("timeout_count") or 0),
            "challenges": int(entry.get("challenge_count") or 0),
            "last_jobs_found": int(entry.get("last_jobs_found") or 0),
            "average_runtime_seconds": float(entry.get("average_runtime_seconds") or 0.0),
            "cooling_down": bool(should_skip),
            "cooldown_reason": reason,
            "last_url": entry.get("last_url") or "",
        })

    return rows


# -- HTTP probe ---------------------------------------------------------------

def _iter_json_ld_entries(data) -> list[dict]:
    if isinstance(data, list):
        entries: list[dict] = []
        for item in data:
            entries.extend(_iter_json_ld_entries(item))
        return entries
    if not isinstance(data, dict):
        return []
    entries = [data]
    graph = data.get("@graph")
    if isinstance(graph, list):
        entries.extend(item for item in graph if isinstance(item, dict))
    return entries


def _json_ld_types(entry: dict) -> set[str]:
    raw_type = entry.get("@type")
    if isinstance(raw_type, list):
        return {str(item).lower() for item in raw_type}
    if raw_type:
        return {str(raw_type).lower()}
    return set()


def _location_from_jobposting(entry: dict) -> str | None:
    location = entry.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if location else None
    if isinstance(location, dict):
        address = location.get("address")
        if isinstance(address, dict):
            parts = [
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("addressCountry"),
            ]
            text = ", ".join(_clean_text(part) for part in parts if _clean_text(part))
            return text or None
        return _clean_text(location.get("name")) or None
    if isinstance(location, str):
        return _clean_text(location) or None
    applicant_location = entry.get("applicantLocationRequirements")
    if isinstance(applicant_location, dict):
        return _clean_text(applicant_location.get("name")) or None
    return None


def _extract_json_ld_jobs(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for entry in _iter_json_ld_entries(data):
            if "jobposting" not in _json_ld_types(entry):
                continue
            url = _clean_text(entry.get("url")) or base_url
            jobs.append({
                "title": _clean_text(entry.get("title")),
                "salary": _coerce_db_scalar(entry.get("baseSalary")),
                "description": _clean_text(entry.get("description")),
                "location": _location_from_jobposting(entry),
                "url": urljoin(base_url, url),
            })
    return jobs


def _extract_static_job_links(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = _clean_text(link.get("href"))
        text = _clean_text(link.get_text(" ", strip=True))
        if not href or not text:
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        if not re.search(r"job|career|opening|role|position", href, re.I):
            continue
        if not _is_valid_smart_title(text):
            continue
        seen.add(full_url)
        jobs.append({
            "title": text,
            "salary": None,
            "description": text,
            "location": None,
            "url": full_url,
        })
    return jobs[:100]


def http_probe_target(name: str, url: str) -> dict:
    """Try cheap static extraction before paying the Playwright cost."""
    t0 = time.time()
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/json",
            },
            timeout=max(5, min(SMART_GOTO_TIMEOUT_MS / 1000, 20)),
        )
        raw_text = resp.text or ""
        issue = detect_page_issue(raw_text, resp.url or url)
        if issue:
            return {
                "name": name,
                "status": "CHALLENGE",
                "strategy": "http_probe",
                "issue_type": issue["type"],
                "error": issue["detail"],
                "challenge": True,
                "jobs": [],
                "validation_counts": {},
                "validated": True,
                "elapsed_seconds": time.time() - t0,
            }
        resp.raise_for_status()
    except Exception as e:
        return {
            "name": name,
            "status": "HTTP_ERROR",
            "strategy": "http_probe",
            "error": str(e),
            "jobs": [],
            "validation_counts": {},
            "validated": True,
            "elapsed_seconds": time.time() - t0,
        }

    content_type = resp.headers.get("content-type", "")
    html = resp.text if "html" in content_type or "<html" in resp.text[:500].lower() else ""
    jobs = _extract_json_ld_jobs(html, resp.url or url) if html else []
    if not jobs and html:
        jobs = _extract_static_job_links(html, resp.url or url)

    valid_jobs, validation_counts = validate_smart_jobs(jobs)
    status = "PASS" if len(valid_jobs) >= SMART_MIN_VALID_JOBS_FOR_SUCCESS else "FAIL"
    titles = sum(1 for job in valid_jobs if job.get("title"))
    return {
        "name": name,
        "status": status,
        "strategy": "http_probe",
        "total": len(valid_jobs),
        "titles": titles,
        "jobs": valid_jobs,
        "validation_counts": validation_counts,
        "validated": True,
        "elapsed_seconds": time.time() - t0,
    }


# -- Location filtering -------------------------------------------------------

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


# -- Site configuration from YAML --------------------------------------------

def load_sites() -> list[dict]:
    """Load scraping target sites from config/sites.yaml."""
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        log.warning("sites.yaml not found at %s", path)
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data.get("sites", [])


def _list_config(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


def _filter_sites(sites: list[dict], smart_cfg: dict) -> list[dict]:
    include = {item.lower() for item in _list_config(smart_cfg.get("include_sites"))}
    exclude = {item.lower() for item in _list_config(smart_cfg.get("exclude_sites"))}
    include_search = _cfg_bool(smart_cfg, "include_search_sites", True)
    include_static = _cfg_bool(smart_cfg, "include_static_sites", True)

    filtered: list[dict] = []
    for site in sites:
        name = str(site.get("name", "")).lower()
        site_type = site.get("type", "static")
        if include and name not in include:
            continue
        if name in exclude:
            continue
        if site_type == "search" and not include_search:
            continue
        if site_type != "search" and not include_static:
            continue
        filtered.append(site)
    return filtered


def _store_jobs_filtered(
    conn: sqlite3.Connection,
    jobs: list[dict],
    site: str,
    strategy: str,
    accept_locs: list[str],
    reject_locs: list[str],
) -> tuple[int, int]:
    """Store jobs with location filtering. Returns (new, existing)."""
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0
    filtered = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        if not _location_ok(job.get("location"), accept_locs, reject_locs):
            filtered += 1
            continue
        # Guard each insert: a single malformed job (e.g. a value SQLite can't
        # bind) must not abort the whole batch.
        try:
            status = insert_discovered_job(
                conn,
                job,
                site=job.get("company") or site,
                strategy=strategy,
                source_board=site,
                discovered_at=now,
            )
        except Exception as e:
            log.warning("Skipping un-storable job %s: %s", str(url)[:80], e)
            continue
        if status == "new":
            new += 1
        elif status in {"existing", "duplicate"}:
            existing += 1

    if filtered:
        log.info("Filtered %d jobs (wrong location)", filtered)
    conn.commit()
    return new, existing


# -- Page intelligence collector ---------------------------------------------

def collect_page_intelligence(url: str, headless: bool = True) -> dict:
    """Load a page with Playwright and collect every signal a scraping engineer
    would look at in DevTools. Returns a structured intelligence report."""
    intel: dict = {
        "url": url,
        "json_ld": [],
        "api_responses": [],
        "data_testids": [],
        "page_title": "",
        "dom_stats": {},
        "card_candidates": [],
    }

    captured_responses: list[dict] = []

    def on_response(response):
        ct = response.headers.get("content-type", "")
        rurl = response.url
        if any(ext in rurl for ext in [".js", ".css", ".png", ".jpg", ".svg", ".woff", ".ico", ".gif", ".webp"]):
            return
        if "json" in ct or "/api/" in rurl or "algolia" in rurl or "graphql" in rurl:
            try:
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > SMART_MAX_RESPONSE_BYTES:
                    return
                body = response.text()
                if len(body) > SMART_MAX_RESPONSE_BYTES:
                    return
                try:
                    data = json.loads(body)
                except Exception:
                    data = None
                captured_responses.append({
                    "url": rurl,
                    "status": response.status,
                    "size": len(body),
                    "data": data,
                })
            except Exception:
                pass

    with sync_playwright() as p:
        browser = None
        launch_opts: dict = {"headless": headless}
        try:
            launch_opts["executable_path"] = config.get_chrome_path()
        except FileNotFoundError:
            pass
        try:
            browser = p.chromium.launch(**launch_opts)
            context = browser.new_context(user_agent=UA)
            page = context.new_page()
            page.set_default_timeout(SMART_ACTION_TIMEOUT_MS)
            page.set_default_navigation_timeout(SMART_GOTO_TIMEOUT_MS)

            if SMART_BLOCK_RESOURCES:
                def _route_static_assets(route):
                    if route.request.resource_type in {"image", "media", "font", "stylesheet"}:
                        return route.abort()
                    return route.continue_()

                page.route("**/*", _route_static_assets)

            page.on("response", on_response)

            try:
                page.goto(url, timeout=SMART_GOTO_TIMEOUT_MS, wait_until="domcontentloaded")
            except PlaywrightTimeoutError:
                log.warning(
                    "Page load timed out after %dms; continuing with current DOM",
                    SMART_GOTO_TIMEOUT_MS,
                )

            if SMART_WAIT_FOR_NETWORK_IDLE and SMART_NETWORK_IDLE_TIMEOUT_MS > 0:
                try:
                    page.wait_for_load_state("networkidle", timeout=SMART_NETWORK_IDLE_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    log.warning(
                        "Network did not go idle after %dms; continuing",
                        SMART_NETWORK_IDLE_TIMEOUT_MS,
                    )

            intel["page_title"] = page.title()

            # 1. JSON-LD
            for el in page.query_selector_all('script[type="application/ld+json"]'):
                try:
                    data = json.loads(el.inner_text())
                    intel["json_ld"].append(data)
                except Exception:
                    pass

            # 2. __NEXT_DATA__
            next_data = page.query_selector("script#__NEXT_DATA__")
            if next_data:
                try:
                    intel["next_data"] = json.loads(next_data.inner_text())
                except Exception:
                    pass

            # 3. data-testid attributes
            intel["data_testids"] = page.evaluate("""
            () => {
                const els = document.querySelectorAll('[data-testid]');
                const results = [];
                els.forEach(el => {
                    results.push({
                        testid: el.getAttribute('data-testid'),
                        tag: el.tagName.toLowerCase(),
                        text: el.innerText?.slice(0, 80) || ''
                    });
                });
                return results.slice(0, 50);
            }
        """)

            # 4. DOM stats
            intel["dom_stats"] = page.evaluate("""
            () => {
                const body = document.body;
                return {
                    total_elements: body.querySelectorAll('*').length,
                    links: body.querySelectorAll('a[href]').length,
                    headings: body.querySelectorAll('h1,h2,h3,h4').length,
                    lists: body.querySelectorAll('ul,ol').length,
                    tables: body.querySelectorAll('table').length,
                    articles: body.querySelectorAll('article').length,
                    has_data_ids: body.querySelectorAll('[data-id]').length,
                };
            }
        """)

            # 5. Find repeating card-like elements
            intel["card_candidates"] = page.evaluate("""
            () => {
                const candidates = [];
                const allParents = document.querySelectorAll('*');

                for (const parent of allParents) {
                    const children = Array.from(parent.children);
                    if (children.length < 3) continue;

                    const tagCounts = {};
                    children.forEach(c => {
                        const key = c.tagName;
                        tagCounts[key] = (tagCounts[key] || 0) + 1;
                    });

                    const dominant = Object.entries(tagCounts).sort((a,b) => b[1]-a[1])[0];
                    if (!dominant || dominant[1] < 3) continue;

                    const repeatingChildren = children.filter(c => c.tagName === dominant[0]);
                    const withText = repeatingChildren.filter(c => c.innerText?.trim().length > 20);
                    if (withText.length < 3) continue;

                    const withLinks = withText.filter(c => c.querySelector('a[href]'));
                    const score = withLinks.length * 2 + withText.length;

                    const parentId = parent.id ? '#' + parent.id : '';
                    const parentClasses = Array.from(parent.classList).filter(c => c.length < 30).slice(0, 3).join('.');
                    const parentTag = parent.tagName.toLowerCase();
                    const parentSelector = parentTag + (parentId || (parentClasses ? '.' + parentClasses : ''));

                    const childTag = dominant[0].toLowerCase();
                    const sampleChild = withText[0];
                    const childClasses = Array.from(sampleChild.classList).filter(c => c.length < 30).slice(0, 3).join('.');
                    const childSelector = childTag + (childClasses ? '.' + childClasses : '');

                    const examples = withText.slice(0, 3).map(c => {
                        const clone = c.cloneNode(true);
                        clone.querySelectorAll('script,style,svg,noscript').forEach(el => el.remove());
                        const html = clone.outerHTML;
                        return html.length > 5000 ? html.slice(0, 5000) + '...' : html;
                    });

                    candidates.push({
                        parent_selector: parentSelector,
                        child_selector: childSelector,
                        child_tag: childTag,
                        total_children: repeatingChildren.length,
                        with_text: withText.length,
                        with_links: withLinks.length,
                        score: score,
                        examples: examples,
                    });
                }

                candidates.sort((a,b) => b.score - a.score);
                return candidates.slice(0, 3);
            }
        """)

            # Capture full rendered HTML
            intel["full_html"] = page.content()
        finally:
            if browser is not None:
                browser.close()

    # Process API responses
    for resp in captured_responses:
        summary: dict = {
            "url": resp["url"][:200],
            "status": resp["status"],
            "size": resp["size"],
            "_raw_data": resp.get("data"),
        }
        data = resp.get("data")
        if data:
            if isinstance(data, list) and data:
                summary["type"] = f"array[{len(data)}]"
                if isinstance(data[0], dict):
                    summary["first_item_keys"] = list(data[0].keys())[:20]
                    summary["first_item_sample"] = {k: str(v)[:100] for k, v in list(data[0].items())[:8]}
            elif isinstance(data, dict):
                summary["type"] = "object"
                summary["keys"] = list(data.keys())[:20]

                def _explore_nested(obj, path_prefix, depth=0):
                    if depth > 3 or not isinstance(obj, dict):
                        return
                    for key in list(obj.keys())[:15]:
                        val = obj[key]
                        path = f"{path_prefix}.{key}" if path_prefix else key
                        if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                            info = {
                                "count": len(val),
                                "first_item_keys": list(val[0].keys())[:20],
                                "first_item_sample": {k: str(v)[:200] for k, v in list(val[0].items())[:8]},
                            }
                            for subkey in list(val[0].keys())[:10]:
                                subval = val[0][subkey]
                                if isinstance(subval, list) and len(subval) > 0 and isinstance(subval[0], dict):
                                    info[f"first_item.{subkey}"] = {
                                        "count": len(subval),
                                        "first_item_keys": list(subval[0].keys())[:15],
                                        "first_item_sample": {k: str(v)[:100] for k, v in list(subval[0].items())[:8]},
                                    }
                                elif isinstance(subval, dict):
                                    info[f"first_item.{subkey}"] = {
                                        "type": "object",
                                        "keys": list(subval.keys())[:15],
                                        "sample": {k: str(v)[:150] for k, v in list(subval.items())[:8]},
                                    }
                            summary[f"nested_{path}"] = info
                        elif isinstance(val, dict) and depth < 3:
                            _explore_nested(val, path, depth + 1)
                _explore_nested(data, "")
        intel["api_responses"].append(summary)

    return intel


# -- Judge: filter API responses ---------------------------------------------

JUDGE_PROMPT = """You are filtering intercepted API responses from a job listings website.
Decide if this API response contains actual job listing data (titles, companies, locations, etc).

API Response Summary:
  URL: {url}
  Status: {status}
  Size: {size} chars
  Type: {type}
  Keys/Fields: {fields}
  Sample: {sample}

Is this job listing data? Answer in under 10 words. Return ONLY valid JSON:
{{"relevant": true, "reason": "job objects with title/company"}}
or
{{"relevant": false, "reason": "auth endpoint"}}

No explanation, no markdown, no thinking."""


def judge_api_responses(api_responses: list[dict]) -> list[dict]:
    """Use the LLM to filter API responses, keeping only job-relevant ones."""
    if not api_responses:
        return []

    client = get_client()
    relevant: list[dict] = []

    for resp in api_responses:
        fields = ""
        sample = ""
        resp_type = resp.get("type", "unknown")
        if "first_item_keys" in resp:
            fields = str(resp["first_item_keys"])
            sample = json.dumps(resp.get("first_item_sample", {}), indent=2)[:500]
        elif "keys" in resp:
            fields = str(resp["keys"])
            for k, v in resp.items():
                if k.startswith("nested_"):
                    fields += f"\n  .{k.replace('nested_', '')}: {v.get('count', '?')} items, keys={v.get('first_item_keys', '?')}"
                    sample = json.dumps(v.get("first_item_sample", {}), indent=2)[:500]
        else:
            fields = "no structured data"

        prompt = JUDGE_PROMPT.format(
            url=resp.get("url", "?")[:200],
            status=resp.get("status", "?"),
            size=resp.get("size", "?"),
            type=resp_type,
            fields=fields,
            sample=sample or "n/a",
        )

        try:
            raw = client.ask(prompt, temperature=0.0, max_tokens=1024, stage="extract")
            verdict = extract_json(raw)
            is_relevant = verdict.get("relevant", False)
            reason = verdict.get("reason", "?")
            log.info("Judge: %s -> %s (%s)", resp.get("url", "?")[:80],
                     "KEEP" if is_relevant else "DROP", reason)
            if is_relevant:
                relevant.append(resp)
        except Exception as e:
            log.warning("Judge ERROR for %s: %s -- keeping", resp.get("url", "?")[:80], e)
            relevant.append(resp)

    return relevant


# -- Phase 1: strategy selection ---------------------------------------------

def format_strategy_briefing(intel: dict) -> str:
    """Lightweight briefing for strategy selection. No raw DOM."""
    sections: list[str] = []
    sections.append(f"PAGE: {intel['url']}")
    sections.append(f"TITLE: {intel['page_title']}")

    # JSON-LD
    if intel["json_ld"]:
        job_postings = [j for j in intel["json_ld"] if isinstance(j, dict) and j.get("@type") == "JobPosting"]
        other = [j for j in intel["json_ld"] if not (isinstance(j, dict) and j.get("@type") == "JobPosting")]
        if job_postings:
            sections.append(f"\nJSON-LD: {len(job_postings)} JobPosting entries found (usable!)")
            sections.append(f"First JobPosting:\n{json.dumps(job_postings[0], indent=2)[:3000]}")
        else:
            sections.append("\nJSON-LD: NO JobPosting entries (json_ld strategy will NOT work)")
        if other:
            types = [j.get("@type", "?") if isinstance(j, dict) else "?" for j in other]
            sections.append(f"Other JSON-LD types (NOT job data): {types}")
    else:
        sections.append("\nJSON-LD: none")

    # API responses
    if intel["api_responses"]:
        sections.append(f"\nAPI RESPONSES INTERCEPTED: {len(intel['api_responses'])} calls")
        for resp in intel["api_responses"]:
            sections.append(f"\n  URL: {resp['url']}")
            sections.append(f"  Status: {resp['status']} | Size: {resp['size']:,} chars | Type: {resp.get('type', '?')}")
            if "first_item_keys" in resp:
                sections.append(f"  Item keys: {resp['first_item_keys']}")
                sections.append(f"  Sample: {json.dumps(resp.get('first_item_sample', {}), indent=2)[:1000]}")
            if "keys" in resp:
                sections.append(f"  Object keys: {resp['keys']}")
            for k, v in resp.items():
                if k.startswith("nested_"):
                    arr_name = k.replace("nested_", "")
                    sections.append(f"  .{arr_name}: array of {v['count']} items")
                    sections.append(f"    Item keys: {v['first_item_keys']}")
                    sections.append(f"    Sample: {json.dumps(v.get('first_item_sample', {}), indent=2)[:1000]}")
                    for sk, sv in v.items():
                        if sk.startswith("first_item.") and isinstance(sv, dict):
                            sub_name = sk.replace("first_item.", "")
                            if "count" in sv:
                                sections.append(f"    .{arr_name}[0].{sub_name}: array of {sv['count']} items")
                                sections.append(f"      Item keys: {sv['first_item_keys']}")
                                sections.append(f"      Sample: {json.dumps(sv.get('first_item_sample', {}), indent=2)[:1500]}")
                            elif "keys" in sv:
                                sections.append(f"    .{arr_name}[0].{sub_name}: object with keys {sv['keys']}")
                                sections.append(f"      Sample: {json.dumps(sv.get('sample', {}), indent=2)[:1500]}")
    else:
        sections.append("\nAPI RESPONSES: none intercepted")

    # data-testid
    if intel["data_testids"]:
        sections.append(f"\nDATA-TESTID ATTRIBUTES: {len(intel['data_testids'])} elements")
        for dt in intel["data_testids"][:15]:
            text_preview = dt['text'].replace('\n', ' ')[:60]
            sections.append(f"  <{dt['tag']} data-testid=\"{dt['testid']}\"> {text_preview}")
    else:
        sections.append("\nDATA-TESTID: none found")

    # DOM stats
    stats = intel.get("dom_stats", {})
    sections.append(f"\nDOM STATS: {stats.get('total_elements', '?')} elements, "
                    f"{stats.get('links', '?')} links, {stats.get('headings', '?')} headings, "
                    f"{stats.get('tables', '?')} tables, {stats.get('articles', '?')} articles, "
                    f"{stats.get('has_data_ids', '?')} data-id elements")

    # Card candidates
    if intel["card_candidates"]:
        sections.append(f"\nREPEATING ELEMENTS DETECTED: {len(intel['card_candidates'])} candidate groups")
        for i, cand in enumerate(intel["card_candidates"]):
            sections.append(f"  [{i}] parent={cand['parent_selector']} child={cand['child_selector']} "
                          f"count={cand['total_children']} with_text={cand['with_text']} with_links={cand['with_links']}")
    else:
        sections.append("\nREPEATING ELEMENTS: none detected")

    return "\n".join(sections)


STRATEGY_PROMPT = """You are analyzing a job listings page to pick the best extraction strategy.

Below is a lightweight intelligence briefing -- JSON-LD data, intercepted API responses, data-testid attributes, and DOM statistics. NO raw DOM HTML is included.

Pick the BEST strategy:

1. "json_ld" -- ONLY if briefing shows JobPosting JSON-LD entries (it will say "usable!")
2. "api_response" -- ONLY if an intercepted API response has job-like fields (name, title, salary, description, location, slug)
3. "css_selectors" -- when neither JSON-LD nor API data has job data

HOW TO THINK:
- If the briefing says "JSON-LD: NO JobPosting entries" or "json_ld strategy will NOT work", do NOT pick json_ld.
- For api_response: "url_pattern" must be a substring that matches one of the INTERCEPTED API URLs listed above (not the page URL!). Copy a unique part of the API URL.
- For api_response: "items_path" must point to the ARRAY of items, not a single item. Use dot notation with [n] ONLY for traversing into a specific index to reach an inner array. Example: if data is {{"results": [{{"hits": [...]}}]}}, items_path is "results[0].hits" to reach the hits array.
- For api_response: field paths (title, salary, etc.) are RELATIVE TO EACH ITEM in the array. If items are nested objects like {{"_source": {{"Title": "..."}}}}, use "_source.Title" for the title field.
- For css_selectors: just return {{"strategy":"css_selectors","reasoning":"...","extraction":{{}}}} -- selectors will be generated in a separate focused step.

Return ONLY valid JSON:

For json_ld:
{{"strategy":"json_ld","reasoning":"...","extraction":{{"title":"title","salary":"baseSalary_path_or_null","description":"description","location":"jobLocation[0].address.addressCountry","url":"url_field"}}}}

For api_response:
{{"strategy":"api_response","reasoning":"...","extraction":{{"url_pattern":"actual.url.substring","items_path":"path.to.the.array","title":"field_in_each_item","salary":"salary_field_or_null","description":"description_field_or_null","location":"location_path","url":"url_field"}}}}

For css_selectors:
{{"strategy":"css_selectors","reasoning":"...","extraction":{{}}}}

Keep reasoning under 20 words. No explanation, no markdown, no code fences.

INTELLIGENCE BRIEFING:
{briefing}"""


# -- Card HTML cleaning (allowlist approach) ----------------------------------

_ALLOWED_ATTRS = {"id", "href", "data-testid", "data-id", "data-type", "data-slug",
                  "role", "aria-label", "aria-labelledby", "type", "name", "for"}
_ALLOWED_PREFIXES = ("data-", "aria-")
_UTILITY_CLASS_RE = re.compile(
    r"^("
    r"[a-z]{1,2}-\d+|"
    r"[a-z]{1,3}-[a-z]{1,3}-\d+|"
    r"col-\d+|"
    r"d-\w+|"
    r"align-\w+|justify-\w+|"
    r"flex-\w+|order-\d+|"
    r"text-\w+|font-\w+|"
    r"bg-\w+|border-\w+|"
    r"rounded-?\w*|shadow-?\w*|"
    r"w-\d+|h-\d+|"
    r"position-\w+|overflow-\w+|"
    r"float-\w+|clearfix|"
    r"visible-\w+|invisible|"
    r"sr-only|"
    r"css-[a-z0-9]+|"
    r"sc-[a-zA-Z]+|"
    r"sc-[a-f0-9]+-\d+"
    r")$"
)


def clean_card_html(html: str) -> str:
    """Strip layout noise from card HTML, keep only what the LLM needs for selectors."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(True):
        new_attrs: dict = {}
        for attr, val in list(tag.attrs.items()):
            if attr in _ALLOWED_ATTRS or any(attr.startswith(p) for p in _ALLOWED_PREFIXES):
                new_attrs[attr] = val
            elif attr == "class":
                classes = val if isinstance(val, list) else val.split()
                kept = [c for c in classes if not _UTILITY_CLASS_RE.match(c)]
                if kept:
                    new_attrs["class"] = kept
        tag.attrs = new_attrs

    return str(soup)


def clean_page_html(html: str, max_chars: int = 150_000) -> str:
    """Strip full page HTML to essential structure for LLM card detection."""
    soup = BeautifulSoup(html, "html.parser")

    main = soup.find("main") or soup.find(attrs={"role": "main"})
    if main and len(str(main)) > 1000:
        soup = BeautifulSoup(str(main), "html.parser")

    for tag in soup.find_all(["script", "style", "svg", "noscript", "iframe",
                              "link", "meta", "head", "footer", "nav"]):
        tag.decompose()

    for tag in soup.find_all(True):
        new_attrs: dict = {}
        for attr, val in list(tag.attrs.items()):
            if attr in _ALLOWED_ATTRS or any(attr.startswith(p) for p in _ALLOWED_PREFIXES):
                new_attrs[attr] = val
            elif attr == "class":
                classes = val if isinstance(val, list) else val.split()
                kept = [c for c in classes if not _UTILITY_CLASS_RE.match(c)]
                if kept:
                    new_attrs["class"] = kept
        tag.attrs = new_attrs

    for tag in soup.find_all(True):
        if not tag.get_text(strip=True) and not tag.find("img") and not tag.find("a"):
            tag.decompose()

    result = str(soup)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n<!-- TRUNCATED -->"
    return result


# -- Phase 2: CSS selector generation ----------------------------------------

FULL_PAGE_SELECTOR_PROMPT = """You are a senior web scraping engineer. Below is the cleaned HTML of a job listings page.

Your task:
1. Find the repeating HTML elements that represent individual job listings
2. Generate CSS selectors to extract data from them

Return a JSON object:
- "job_card": CSS selector matching each job card (MUST match ALL cards on the page)
- "title": selector RELATIVE to the card for the job title
- "salary": selector relative to card for salary, or null
- "description": selector relative to card for description snippet, or null
- "location": selector relative to card for location, or null
- "url": selector relative to card for the link (<a> tag) to the job detail page

Selector rules:
- SIMPLEST wins. A single attribute selector like [data-testid="job-card"] is better than a multi-level path like li > div > [data-testid="job-card"]. Do NOT add parent/ancestor selectors unless the target is ambiguous without them.
- For data-testid/data-id with DYNAMIC values (e.g. data-testid="card-123"), use prefix matching: [data-testid^="card-"]
- For data-testid with STATIC values (e.g. data-testid="job-card"), use exact: [data-testid="job-card"]
- Prefer semantic HTML: article, section, h2, h3 over div
- NEVER use hashed/generated classes: sc-*, css-*, random 5-8 char strings like "fJyWhK"
- Max 2 levels deep. One level is best.
- The "url" selector should target an <a> element (we extract its href attribute)
- If the page has NO job listings visible, return {{"error": "no job listings found"}}

Return ONLY valid JSON, no explanation, no markdown.

PAGE HTML:
{page_html}"""


# -- LLM helpers -------------------------------------------------------------

def ask_llm(prompt: str) -> tuple[str, float, dict]:
    """Send prompt to LLM. Returns (response_text, seconds_taken, metadata)."""
    client = get_client()
    t0 = time.time()
    text = client.ask(prompt, temperature=0.0, max_tokens=4096, stage="extract")
    elapsed = time.time() - t0
    meta = {
        "finish_reason": "stop",
        "prompt_chars": len(prompt),
        "response_chars": len(text),
    }
    return text, elapsed, meta


def extract_json(text: str) -> dict:
    """Extract JSON from LLM response, handling think tags and code fences."""
    if "<think>" in text:
        after = text.split("</think>")[-1].strip()
        if after:
            text = after
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    text = text.strip()
    text = re.sub(r'\\([^"\\\/bfnrtu])', r'\1', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    while text.endswith("}") or text.endswith("]"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            text = text[:-1].rstrip()
    raise json.JSONDecodeError("Could not parse JSON", text, 0)


# -- JSON path resolution ---------------------------------------------------

def resolve_json_path_raw(data, path: str):
    """Navigate a JSON path and return whatever is there (including lists/dicts)."""
    if not path or not data:
        return None
    try:
        current = data
        for part in path.replace("[", ".[").split("."):
            if not part:
                continue
            if part.startswith("[") and part.endswith("]"):
                idx = int(part[1:-1])
                current = current[idx]
            else:
                current = current[part]
        return current
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def resolve_json_path(data, path: str):
    """Simple JSON path resolver with type coercion for display."""
    if not path or not data:
        return None
    try:
        current = data
        for part in path.replace("[", ".[").split("."):
            if not part:
                continue
            if part.startswith("[") and part.endswith("]"):
                idx = int(part[1:-1])
                current = current[idx]
            else:
                current = current[part]
        if isinstance(current, (str, int, float)):
            return str(current) if not isinstance(current, str) else current
        elif isinstance(current, dict):
            return current.get("name", current.get("text", str(current)[:100]))
        elif isinstance(current, list):
            if current and isinstance(current[0], dict):
                return ", ".join(str(item.get("name", item.get("text", ""))) for item in current[:3])
            return ", ".join(str(x) for x in current[:3])
        return str(current) if current else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None


# -- Extraction executors ----------------------------------------------------

def execute_json_ld(intel: dict, plan: dict) -> list[dict]:
    """Extract jobs from JSON-LD JobPosting entries."""
    ext = plan["extraction"]
    jobs: list[dict] = []
    for entry in intel["json_ld"]:
        if not isinstance(entry, dict) or entry.get("@type") != "JobPosting":
            continue
        job: dict = {}
        for field in ["title", "salary", "description", "location", "url"]:
            path = ext.get(field)
            if not path or path == "null":
                job[field] = None
                continue
            job[field] = resolve_json_path(entry, path)
        jobs.append(job)
    return jobs


def execute_api_response(intel: dict, plan: dict) -> list[dict]:
    """Extract jobs from intercepted API response data."""
    ext = plan["extraction"]
    url_pattern = ext.get("url_pattern", "")

    target_data = None
    for resp in intel["api_responses"]:
        if url_pattern in resp.get("url", ""):
            target_data = resp.get("_raw_data")
            break

    if not target_data:
        log.warning("Could not find stored API response matching: %s", url_pattern)
        return []

    items_path = ext.get("items_path", "")
    items = resolve_json_path_raw(target_data, items_path)
    if not isinstance(items, list):
        log.warning("items_path '%s' did not resolve to a list (got %s)", items_path, type(items).__name__)
        return []

    jobs: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        job: dict = {}
        for field in ["title", "salary", "description", "location", "url"]:
            path = ext.get(field)
            if not path or path == "null":
                job[field] = None
                continue
            job[field] = resolve_json_path(item, path)
        jobs.append(job)
    return jobs


def execute_css_selectors(intel: dict) -> tuple[dict, list[dict]]:
    """Phase 2: Send full cleaned page HTML to LLM for card detection + selector generation.
    Returns (selectors, jobs)."""
    full_html = intel.get("full_html", "")
    if not full_html:
        log.warning("No page HTML captured")
        return {}, []

    cleaned = clean_page_html(full_html)
    log.info("Page HTML: %s -> %s chars", f"{len(full_html):,}", f"{len(cleaned):,}")

    prompt = FULL_PAGE_SELECTOR_PROMPT.format(page_html=cleaned)

    try:
        raw, elapsed, meta = ask_llm(prompt)
    except Exception as e:
        log.error("LLM_ERROR in Phase 2: %s", e)
        return {}, []

    log.info("Phase 2 LLM: %d chars, %.1fs", meta['response_chars'], elapsed)

    try:
        selectors = extract_json(raw)
    except Exception as e:
        log.error("PARSE_ERROR in Phase 2: %s | raw: %s", e, raw[:500])
        return {}, []

    if "error" in selectors:
        log.warning("LLM: %s", selectors["error"])
        return selectors, []

    log.info("Selectors: %s", selectors)

    # Apply selectors to the ORIGINAL full_html
    soup = BeautifulSoup(full_html, "html.parser")
    card_sel = selectors.get("job_card", "NONE")
    try:
        cards = soup.select(card_sel)
    except Exception as e:
        log.error("Invalid card selector '%s': %s", card_sel, e)
        return selectors, []

    log.info("Matched %d cards", len(cards))

    jobs: list[dict] = []
    for card in cards:
        job: dict = {}
        for field in ["title", "salary", "description", "location", "url"]:
            sel = selectors.get(field)
            if not sel or sel == "null":
                job[field] = None
                continue
            try:
                el = card.select_one(sel)
            except Exception:
                job[field] = None
                continue
            if el:
                job[field] = el.get("href") if field == "url" else el.get_text(strip=True)
            else:
                job[field] = None
        jobs.append(job)
    return selectors, jobs


# -- Main per-site extraction ------------------------------------------------

def _run_one_site(name: str, url: str) -> dict:
    """Run full smart extraction pipeline on one site URL."""
    log.info("=" * 60)
    log.info("%s: %s", name, url)

    # Step 1: Collect intelligence
    log.info("[1] Collecting page intelligence...")
    t0 = time.time()
    try:
        intel = collect_page_intelligence(url)
    except Exception as e:
        log.error("COLLECT_ERROR: %s", e)
        return {"name": name, "status": "COLLECT_ERROR", "error": str(e), "jobs": []}
    collect_time = time.time() - t0
    log.info("Done in %.1fs | JSON-LD: %d | API: %d | testids: %d | cards: %d",
             collect_time, len(intel["json_ld"]), len(intel["api_responses"]),
             len(intel["data_testids"]), len(intel["card_candidates"]))

    # Headful retry if page content is tiny
    full_html = intel.get("full_html", "")
    cleaned_check = clean_page_html(full_html) if full_html else ""
    issue = detect_page_issue(full_html, url)
    if issue:
        log.warning("%s detected for %s -- skipping extraction", issue["label"], name)
        return {
            "name": name,
            "status": "CHALLENGE",
            "strategy": "page_intelligence",
            "issue_type": issue["type"],
            "error": issue["detail"],
            "challenge": True,
            "jobs": [],
        }
    if SMART_HEADFUL_RETRY and len(cleaned_check) < 5000 and full_html:
        log.info("Cleaned HTML only %s chars -- retrying headful...", f"{len(cleaned_check):,}")
        try:
            intel = collect_page_intelligence(url, headless=False)
            collect_time = time.time() - t0
            log.info("Headful done in %.1fs | JSON-LD: %d | API: %d",
                     collect_time, len(intel["json_ld"]), len(intel["api_responses"]))
        except Exception as e:
            log.warning("Headful retry failed: %s", e)

    # Step 1.5: Judge filters API responses
    if intel["api_responses"]:
        log.info("[1.5] Judge filtering API responses...")
        intel["api_responses"] = judge_api_responses(intel["api_responses"])
        log.info("Kept %d relevant responses", len(intel["api_responses"]))

    # Step 2: Strategy selection
    briefing = format_strategy_briefing(intel)
    log.info("[2] Phase 1: Strategy selection (%s chars briefing)", f"{len(briefing):,}")

    prompt = STRATEGY_PROMPT.format(briefing=briefing)
    try:
        raw, elapsed, meta = ask_llm(prompt)
    except Exception as e:
        log.error("LLM_ERROR: %s", e)
        return {"name": name, "status": "LLM_ERROR", "error": str(e)}

    log.info("LLM: %d chars, %.1fs", meta["response_chars"], elapsed)

    try:
        plan = extract_json(raw)
    except Exception as e:
        log.error("PARSE_ERROR: %s | raw: %s", e, raw[:500])
        return {"name": name, "status": "PARSE_ERROR", "error": str(e), "raw": raw}

    if not isinstance(plan, dict):
        # A non-object payload (list/str) would raise AttributeError on .get
        # below, outside the try -- classify it as a parse error and keep raw.
        log.error("PARSE_ERROR: strategy plan was %s, not an object | raw: %s",
                  type(plan).__name__, raw[:500])
        return {"name": name, "status": "PARSE_ERROR",
                "error": f"plan is {type(plan).__name__}, expected object", "raw": raw}

    strategy = plan.get("strategy", "?")
    reasoning = plan.get("reasoning", "?")
    log.info("Strategy: %s | Reasoning: %s", strategy, reasoning)

    # Step 3: Execute
    log.info("[3] Executing %s...", strategy)
    try:
        if strategy == "json_ld":
            log.info("Extraction plan: %s", json.dumps(plan.get("extraction", {}))[:300])
            jobs = execute_json_ld(intel, plan)
        elif strategy == "api_response":
            log.info("Extraction plan: %s", json.dumps(plan.get("extraction", {}))[:300])
            jobs = execute_api_response(intel, plan)
        elif strategy == "css_selectors":
            log.info("-> Phase 2: Generating selectors from card examples...")
            selectors, jobs = execute_css_selectors(intel)
            plan["extraction"] = selectors
        else:
            log.warning("Unknown strategy: %s", strategy)
            jobs = []
    except Exception as e:
        log.error("EXECUTION_ERROR: %s", e)
        return {"name": name, "status": "EXEC_ERROR", "error": str(e), "plan": plan}

    # Step 4: Report
    titles = sum(1 for j in jobs if j.get("title"))
    total = len(jobs)
    status = "PASS" if total > 0 and titles / max(total, 1) >= 0.8 else "FAIL" if total == 0 else "PARTIAL"

    urls = sum(1 for j in jobs if j.get("url"))
    salaries = sum(1 for j in jobs if j.get("salary"))
    descs = sum(1 for j in jobs if j.get("description"))
    log.info("RESULT: %s -- %d jobs, %d titles, %d urls, %d salaries, %d descriptions",
             status, total, titles, urls, salaries, descs)

    for j in jobs[:3]:
        log.info("  - %s | loc: %s | salary: %s",
                 str(j.get("title") or "?")[:55],
                 str(j.get("location") or "?")[:25],
                 str(j.get("salary") or "-")[:20])

    return {
        "name": name,
        "status": status,
        "strategy": strategy,
        "total": total,
        "titles": titles,
        "plan": plan,
        "jobs": jobs,
        "sample": jobs[:5],
    }


def _run_target_with_fallback(target: dict) -> dict:
    """Run one Smart Extract target with cheap HTTP probing before Playwright."""
    started = time.time()
    name = target["name"]
    url = target["url"]

    if SMART_HTTP_PROBE_ENABLED:
        probe_result = http_probe_target(name, url)
        if probe_result.get("status") == "PASS":
            log.info("HTTP probe found %d valid jobs for %s", len(probe_result.get("jobs", [])), name)
            probe_result.setdefault("elapsed_seconds", time.time() - started)
            return probe_result
        log.info(
            "HTTP probe did not produce valid jobs for %s (%s); falling back to browser",
            name,
            probe_result.get("status"),
        )

    result = _run_one_site(name, url)
    result.setdefault("elapsed_seconds", time.time() - started)
    return result


def _is_hard_failure(result: dict) -> bool:
    status = str(result.get("status") or "").upper()
    if status in {"CHALLENGE", "COLLECT_ERROR", "LLM_ERROR", "PARSE_ERROR", "EXEC_ERROR", "ERROR"}:
        return True
    error = str(result.get("error") or "").lower()
    return "timeout" in error or "timed out" in error or "browser" in error


def _result_issue_type(result: dict) -> str:
    issue_type = str(result.get("issue_type") or "").strip()
    if issue_type:
        return issue_type
    if _is_timeout_result(result):
        return "timeout"
    status = str(result.get("status") or "").lower()
    if "http" in status:
        return "http_error"
    if _is_hard_failure(result):
        return "extract_error"
    return ""


def _is_challenge_result(result: dict) -> bool:
    issue_type = _result_issue_type(result)
    return bool(result.get("challenge")) or "challenge" in issue_type


def _is_timeout_result(result: dict) -> bool:
    status = str(result.get("status") or "").lower()
    error = str(result.get("error") or "").lower()
    return "timeout" in status or "timeout" in error or "timed out" in error


# -- Target building --------------------------------------------------------

def build_scrape_targets(
    sites: list[dict] | None = None,
    search_cfg: dict | None = None,
    smart_cfg: dict | None = None,
) -> list[dict]:
    """Build the full list of (name, url) targets from sites + search config queries.

    - "search" sites get expanded: 1 URL per query from search config
    - "static" sites get scraped once as-is

    Placeholders in URLs:
      {query_encoded} -> URL-encoded search query
      {location_encoded} -> URL-encoded location
      {query} -> raw search query (for simple substitution)
    """
    if sites is None:
        sites = load_sites()
    if search_cfg is None:
        search_cfg = config.load_search_config()
    smart_cfg = smart_cfg or {}

    queries_cfg = search_cfg.get("queries", [])
    queries = [q["query"] for q in queries_cfg]
    max_search_queries = _cfg_int(smart_cfg, "max_search_queries", 0)
    if max_search_queries > 0:
        queries = queries[:max_search_queries]
    locs = search_cfg.get("locations", [])
    default_location = locs[0]["location"] if locs else ""

    targets: list[dict] = []

    for site in sites:
        site_url = site.get("url", "")
        site_name = site.get("name", "Unknown")
        site_type = site.get("type", "static")

        if site_type == "search" and queries:
            for query in queries:
                expanded_url = site_url
                expanded_url = expanded_url.replace("{query_encoded}", quote_plus(query))
                expanded_url = expanded_url.replace("{query}", quote_plus(query))
                expanded_url = expanded_url.replace("{location_encoded}", quote_plus(default_location))
                targets.append({
                    "name": site_name,
                    "url": expanded_url,
                    "query": query,
                })
        else:
            expanded_url = site_url
            expanded_url = expanded_url.replace("{location_encoded}", quote_plus(default_location))
            targets.append({
                "name": site_name,
                "url": expanded_url,
                "query": None,
            })

    max_targets = _cfg_int(smart_cfg, "max_targets", 0)
    if max_targets > 0:
        targets = targets[:max_targets]

    return targets


# -- Run all sites -----------------------------------------------------------

def _run_all(
    targets: list[dict],
    accept_locs: list[str],
    reject_locs: list[str],
    workers: int = 1,
) -> dict:
    """Run smart extract on all targets.

    Sequential by default. When workers > 1, scrapes multiple sites in parallel
    using ThreadPoolExecutor. DB storage is still serialized after each result.
    """
    conn = init_db()
    pre_stats = get_stats(conn)
    log.info("Database: %d jobs already stored, %d pending detail scrape",
             pre_stats["total"], pre_stats["pending_detail"])

    results: list[dict] = []
    total_new = 0
    total_existing = 0
    total_skipped = 0
    validation_totals: Counter[str] = Counter()
    health = load_smart_health() if SMART_HEALTH_ENABLED else {}

    def _process_result(r: dict, target: dict) -> None:
        nonlocal total_new, total_existing
        jobs = r.get("jobs", [])
        if jobs and not r.get("validated"):
            jobs, validation_counts = validate_smart_jobs(jobs)
            r["jobs"] = jobs
            r["validation_counts"] = validation_counts
            r["validated"] = True
            r["total"] = len(jobs)
            r["titles"] = sum(1 for job in jobs if job.get("title"))
            if not jobs and r.get("status") in {"PASS", "PARTIAL"}:
                r["status"] = "FAIL"
        validation_totals.update(r.get("validation_counts") or {})
        if jobs:
            new, existing = _store_jobs_filtered(conn, jobs, target["name"],
                                                  r.get("strategy", "?"),
                                                  accept_locs, reject_locs)
            total_new += new
            total_existing += existing
            log.info("DB: +%d new, %d already existed", new, existing)

    def _record_health(r: dict, target: dict) -> None:
        if not SMART_HEALTH_ENABLED or r.get("status") == "SKIPPED":
            return
        record_source_health(
            health,
            target["name"],
            url=target["url"],
            status=str(r.get("status") or "UNKNOWN"),
            strategy=r.get("strategy"),
            jobs_found=len(r.get("jobs") or []),
            elapsed_seconds=float(r.get("elapsed_seconds") or 0.0),
            hard_failure=_is_hard_failure(r),
            timeout=_is_timeout_result(r),
            issue_type=_result_issue_type(r),
            challenge=_is_challenge_result(r),
        )

    def _prepare_target(target: dict) -> dict | None:
        nonlocal total_skipped
        if not SMART_HEALTH_ENABLED:
            return None
        should_skip, reason = should_skip_source(
            health,
            target["name"],
            skip_after_failures=SMART_SKIP_AFTER_FAILURES,
            cooldown_hours=SMART_SKIP_COOLDOWN_HOURS,
            now=datetime.now(timezone.utc),
        )
        if not should_skip:
            return None
        total_skipped += 1
        log.warning("Skipping %s: %s", target["name"], reason)
        return make_skip_result(target["name"], reason)

    if workers > 1 and len(targets) > 1:
        # Parallel mode
        with ThreadPoolExecutor(max_workers=min(workers, len(targets))) as pool:
            future_to_target = {}
            for target in targets:
                skip_result = _prepare_target(target)
                if skip_result:
                    results.append(skip_result)
                    continue
                future_to_target[pool.submit(_run_target_with_fallback, target)] = target
            for future in as_completed(future_to_target):
                target = future_to_target[future]
                try:
                    r = future.result()
                except Exception as e:
                    log.error("%s failed: %s", target["name"], e)
                    r = {"name": target["name"], "status": "ERROR", "error": str(e), "jobs": []}
                results.append(r)
                _process_result(r, target)
                _record_health(r, target)
    else:
        # Sequential mode (default)
        for i, target in enumerate(targets):
            label = target["name"]
            if target.get("query"):
                label = f"{target['name']} [{target['query']}]"
            log.info("[%d/%d] %s", i + 1, len(targets), label)

            skip_result = _prepare_target(target)
            if skip_result:
                results.append(skip_result)
                continue

            try:
                r = _run_target_with_fallback(target)
            except Exception as e:
                log.error("%s failed: %s", target["name"], e)
                r = {"name": target["name"], "status": "ERROR", "error": str(e), "jobs": []}
            results.append(r)
            _process_result(r, target)
            _record_health(r, target)

    if SMART_HEALTH_ENABLED:
        save_smart_health(health)

    # Summary
    for r in results:
        strategy = r.get("strategy", "?")
        if r["status"] in ("PASS", "PARTIAL", "FAIL"):
            detail = f"{r.get('total', 0)} jobs, {r.get('titles', 0)} titles, strategy={strategy}"
        elif r["status"] == "SKIPPED":
            detail = r.get("skip_reason", "")[:60]
        else:
            detail = r.get("error", "")[:60]
        log.info("%-10s | %-25s | %s", r["status"], r["name"], detail)

    passed = sum(1 for r in results if r["status"] == "PASS")
    if validation_totals:
        log.info("Validation: %s", ", ".join(
            f"{key}={value}" for key, value in sorted(validation_totals.items())
        ))
    if total_skipped:
        log.info("Skipped %d source target(s) due to Smart Extract health cooldown", total_skipped)
    if SMART_HEALTH_ENABLED:
        problem_rows = [
            row for row in summarize_smart_health(health)
            if row["cooling_down"] or row["issue_type"] or row["timeouts"] or row["challenges"]
        ]
        if problem_rows:
            log.warning("Smart Extract source health issues:")
            for row in problem_rows[:10]:
                issue = row["issue_type"] or row["status"] or "issue"
                cooldown = " cooling_down" if row["cooling_down"] else ""
                log.warning(
                    "  %s: %s, failures=%d, timeouts=%d, challenges=%d%s",
                    row["source"],
                    issue,
                    row["failures"],
                    row["timeouts"],
                    row["challenges"],
                    cooldown,
                )
            if len(problem_rows) > 10:
                log.warning("  ... %d more source health issue(s)", len(problem_rows) - 10)
    log.info("%d/%d PASS", passed, len(results))

    return {"total_new": total_new, "total_existing": total_existing,
            "passed": passed, "total": len(results), "skipped": total_skipped,
            "validation": dict(validation_totals)}


# -- Public entry point ------------------------------------------------------

def run_smart_extract(
    sites: list[dict] | None = None,
    workers: int = 1,
) -> dict:
    """Main entry point for AI-powered smart extraction.

    Loads sites from config/sites.yaml and search queries from the user's
    search config, then runs the extraction pipeline on all targets.

    Args:
        sites: Override the site list. If None, loads from YAML.
        workers: Number of parallel threads for site scraping. Default 1 (sequential).

    Returns:
        Dict with stats: total_new, total_existing, passed, total.
    """
    search_cfg = config.load_search_config()
    smart_cfg = search_cfg.get("smartextract") or search_cfg.get("smart_extract") or {}
    smart_enabled = smart_cfg.get("enabled", True) if isinstance(smart_cfg, dict) else True
    if isinstance(smart_enabled, str):
        smart_enabled = smart_enabled.lower() not in {"0", "false", "no", "off"}
    if not smart_enabled:
        log.info("Smart extract disabled in searches.yaml")
        return {"total_new": 0, "total_existing": 0, "passed": 0, "total": 0}
    if not isinstance(smart_cfg, dict):
        smart_cfg = {}

    _apply_smart_config(smart_cfg)

    accept_locs, reject_locs = _load_location_filter(search_cfg)

    selected_sites = _filter_sites(sites or load_sites(), smart_cfg)
    targets = build_scrape_targets(sites=selected_sites, search_cfg=search_cfg, smart_cfg=smart_cfg)

    if not targets:
        log.warning("No scrape targets configured. Create config/sites.yaml and searches.yaml.")
        return {"total_new": 0, "total_existing": 0, "passed": 0, "total": 0}

    search_sites = sum(1 for s in selected_sites if s.get("type") == "search")
    static_sites = sum(1 for s in selected_sites if s.get("type") != "search")
    log.info("Sites: %d searchable, %d static | Total targets: %d (workers=%d)",
             search_sites, static_sites, len(targets), workers)
    log.info(
        "Smart extract timeouts: goto=%dms action=%dms networkidle=%s",
        SMART_GOTO_TIMEOUT_MS,
        SMART_ACTION_TIMEOUT_MS,
        SMART_NETWORK_IDLE_TIMEOUT_MS if SMART_WAIT_FOR_NETWORK_IDLE else "off",
    )

    return _run_all(targets, accept_locs, reject_locs, workers=workers)
