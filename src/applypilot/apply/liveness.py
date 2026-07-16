"""Pre-apply posting-liveness gate — strictly READ-ONLY toward employers.

Decides whether a job posting is still LIVE / DEAD / UNCERTAIN using per-ATS
public JSON APIs where they exist (Greenhouse, Lever, Ashby, Workday CXS) and
conservative HTTP-status / closure-text / JSON-LD rules elsewhere.

Design contract (do not relax):
  * READ-ONLY toward employers: GET / public JSON APIs only. NEVER POST,
    submit, fill a form, or log in. Never fetch a Lever "/apply" path.
  * Default-safe: a posting is DEAD only on a HIGH-CONFIDENCE signal
    (404/410 on the canonical resource, an ATS API 404 against a *confirmed*
    board, or explicit "expired/closed" page text). Anything blocked /
    SPA-shell / ambiguous / aggregator-stale => UNCERTAIN (kept).
  * NEVER deletes a job. `verify_jobs` only stamps liveness_status /
    last_verified_live / liveness_reason. Dead rows remain in the DB.
  * Polite: bounded per-host concurrency + per-host min-interval. Realistic UA.

This module was ported from the validated standalone measurement probe
(2026-06-22 sweep: 2,874 priority+recommended postings, 15.1% dead; dominant
dead classes independently re-verified — Amazon 404s, themuse archived banner,
Greenhouse board-confirmed API 404, Workday CXS 404 on a proven path).
"""
from __future__ import annotations

import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BODY_CAP = 250_000
TIMEOUT = 15

CLOSED_PATTERNS = [
    "sorry, this job has expired", "this job has expired",
    "this position has expired", "no longer accepting applications",
    "no longer accepting application", "this job is no longer available",
    "this position is no longer available", "this job posting is no longer available",
    "the job you are looking for is no longer", "this job posting is no longer active",
    "this requisition is no longer active", "position has been filled",
    "this position has been filled", "this posting has closed",
    "this position has been closed", "applications are now closed",
    "applications for this job are closed", "this job is closed",
    "job not found", "the page you requested could not be found",
    "this requisition is closed", "no longer open for applications",
]
CLOSED_RE = re.compile("|".join(re.escape(p) for p in CLOSED_PATTERNS), re.I)
VALID_THROUGH_RE = re.compile(r'"validThrough"\s*:\s*"([^"]+)"')
DATE_POSTED_RE = re.compile(r'"datePosted"\s*:\s*"([^"]+)"')
ACCESS_GATE_MARKERS = (
    ("cloudflare", ("cf-ray", "challenges.cloudflare.com"), ("checking your browser",)),
    ("captcha", ("px-captcha", "captcha-delivery.com"), ("captcha", "verify you are human")),
    ("access_denied", (), ("access denied", "request blocked", "enable cookies to continue")),
)

SPA_HOSTS = {"eightfold.ai", "oraclecloud.com", "careerpuck.com", "siriusxm.com",
             "icims.com", "avature.net"}
BLOCKED_HOSTS = {"indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com"}
AGGREGATOR_HOSTS = {"remotejobs.org", "themuse.com", "talent.com", "builtin.com",
                    "click.appcast.io", "jsv3.recruitics.com", "jobs.gem.com",
                    "chiefofstaffjob.com"}

INTERVAL_DEFAULT = 0.7
INTERVAL_OVERRIDES = {"amazon.jobs": 0.8}
CONC_DEFAULT = 2

LIVE, DEAD, UNCERTAIN = "live", "dead", "uncertain"


class _VisibleTextParser(HTMLParser):
    _HIDDEN_TAGS = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() in self._HIDDEN_TAGS:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._HIDDEN_TAGS and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth and data.strip():
            self.parts.append(data.strip())


def _visible_text(body: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(body)
        parser.close()
    except Exception:
        # HTMLParser is deliberately tolerant, but malformed markup should never
        # break a batch or become a reason to declare a posting dead.
        return ""
    return " ".join(parser.parts)


def host_of(url: str) -> str:
    h = (urllib.parse.urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def base_host(h: str) -> str:
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


class _Throttle:
    def __init__(self):
        self.last: dict[str, float] = {}
        self.locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self.sems: dict[str, threading.Semaphore] = {}
        self._sem_lock = threading.Lock()

    def _sem(self, h: str) -> threading.Semaphore:
        with self._sem_lock:
            if h not in self.sems:
                n = 1 if h.endswith("myworkdayjobs.com") else CONC_DEFAULT
                self.sems[h] = threading.Semaphore(n)
            return self.sems[h]

    def _interval(self, h: str) -> float:
        if h.endswith("myworkdayjobs.com"):
            return 1.6
        return INTERVAL_OVERRIDES.get(h, INTERVAL_DEFAULT)

    def acquire(self, h: str) -> None:
        self._sem(h).acquire()
        iv = self._interval(h)
        with self.locks[h]:
            wait = iv - (time.monotonic() - self.last.get(h, 0.0))
            if wait > 0:
                time.sleep(wait)
            self.last[h] = time.monotonic()

    def release(self, h: str) -> None:
        self._sem(h).release()


_THROTTLE = _Throttle()
_SSL_CTX = ssl.create_default_context()
_UNVERIFIED_CTX = ssl._create_unverified_context()


def _open(url: str, accept: str | None = None) -> tuple[int, str, str]:
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
               "Accept": accept or "text/html,application/xhtml+xml,*/*;q=0.8"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX)
    except ssl.SSLError:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT, context=_UNVERIFIED_CTX)
    with resp:
        return resp.status, resp.geturl(), resp.read(BODY_CAP).decode("utf-8", "ignore")


def _fetch(url: str, accept: str | None = None) -> tuple[int, str, str]:
    h = host_of(url)
    _THROTTLE.acquire(h)
    try:
        return _open(url, accept=accept)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(BODY_CAP).decode("utf-8", "ignore")
        except Exception:
            body = ""
        return e.code, getattr(e, "url", url), body
    finally:
        _THROTTLE.release(h)


def _classify_body(
    status: int,
    final_url: str,
    body: str,
    *,
    meta: dict[str, str | None] | None = None,
    requested_url: str | None = None,
) -> tuple[str, str]:
    if status in (404, 410):
        return DEAD, f"http_{status}"
    if status in (401, 403, 429, 999):
        return UNCERTAIN, f"blocked_{status}"
    if status >= 500:
        return UNCERTAIN, f"server_{status}"
    if status != 200:
        return UNCERTAIN, f"http_{status}"
    if requested_url and final_url != requested_url:
        final_path = urllib.parse.urlparse(final_url).path.casefold().rstrip("/")
        if re.search(r"(^|/)(?:login|log-in|signin|sign-in|auth|sso)(?:/|$)", final_path):
            return UNCERTAIN, "redirect_login"
        requested_path = urllib.parse.urlparse(requested_url).path.rstrip("/")
        if requested_path and requested_path != "/" and final_path in ("", "/"):
            return UNCERTAIN, "redirect_home"
    normalized = body.casefold().strip()
    if not normalized:
        return UNCERTAIN, "empty_body"
    visible_text = _visible_text(body)
    normalized_visible = visible_text.casefold()
    for kind, raw_markers, visible_markers in ACCESS_GATE_MARKERS:
        if (
            any(marker in normalized for marker in raw_markers)
            or any(marker in normalized_visible for marker in visible_markers)
        ):
            return UNCERTAIN, f"access_gate:{kind}"
    m = CLOSED_RE.search(visible_text)
    if m:
        return DEAD, f"text:{m.group(0)[:40]!r}"
    if meta is not None:
        dp = DATE_POSTED_RE.search(body)
        if dp and not meta.get("posted_at"):
            meta["posted_at"] = dp.group(1).strip()
    vt = VALID_THROUGH_RE.search(body)
    if vt:
        if meta is not None and not meta.get("valid_through"):
            meta["valid_through"] = vt.group(1).strip()
        try:
            s = vt.group(1).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.date() < datetime.now(timezone.utc).date():
                # Aggregators keep pages live past their own (guessed)
                # validThrough — too weak to drop when the page still 200s.
                if base_host(host_of(final_url)) in AGGREGATOR_HOSTS:
                    return UNCERTAIN, f"jsonld_expired_aggregator_keep_{vt.group(1)[:10]}"
                return DEAD, f"jsonld_validThrough_{vt.group(1)[:10]}"
        except Exception:
            pass
    if base_host(host_of(final_url)) in SPA_HOSTS and len(body) < 1500:
        return UNCERTAIN, "spa_shell"
    if len(normalized) < 80:
        return UNCERTAIN, "thin_body"
    return LIVE, "ok_200"


def _greenhouse(url: str, *, meta: dict[str, str | None] | None = None) -> tuple[str, str]:
    host = host_of(url)
    if host == "grnh.se":
        st, final, body = _fetch(url)
        if host_of(final) != "grnh.se":
            return _dispatch(final, meta=meta)
        return _classify_body(st, final, body, meta=meta)
    p = urllib.parse.urlparse(url)
    parts = [x for x in p.path.split("/") if x]
    qs = urllib.parse.parse_qs(p.query)
    token = job_id = None
    if "jobs" in parts:
        i = parts.index("jobs")
        if i >= 1:
            token = parts[i - 1]
        if i + 1 < len(parts):
            job_id = re.sub(r"\D", "", parts[i + 1]) or parts[i + 1]
    if not token and "gh_jid" in qs:
        return _classify_body(*_fetch(url), meta=meta, requested_url=url)
    if token and job_id:
        api = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"
        st, final, body = _fetch(api, accept="application/json")
        if st == 200:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return UNCERTAIN, "gh_api_invalid_json"
            if not isinstance(payload, dict) or payload.get("id") is None:
                return UNCERTAIN, "gh_api_invalid_payload"
            if str(payload["id"]) != str(job_id):
                return UNCERTAIN, "gh_api_id_mismatch"
            return LIVE, "gh_api_200"
        if st in (404, 410):
            stl, _, _ = _fetch(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                               accept="application/json")
            if stl == 200:
                return DEAD, "gh_api_404"
            return UNCERTAIN, "gh_token_unconfirmed"
        return _classify_body(st, final, body, meta=meta)
    return _classify_body(*_fetch(url), meta=meta, requested_url=url)


def _lever(url: str, *, meta: dict[str, str | None] | None = None) -> tuple[str, str]:
    p = urllib.parse.urlparse(url)
    parts = [x for x in p.path.split("/") if x and x != "apply"]
    if len(parts) >= 2:
        site, jid = parts[0], parts[1]
        st, final, body = _fetch(f"https://api.lever.co/v0/postings/{site}/{jid}",
                                 accept="application/json")
        if st == 200:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return UNCERTAIN, "lever_api_invalid_json"
            if not isinstance(payload, dict) or payload.get("id") is None:
                return UNCERTAIN, "lever_api_invalid_payload"
            if str(payload["id"]) != str(jid):
                return UNCERTAIN, "lever_api_id_mismatch"
            return LIVE, "lever_api_200"
        if st in (404, 410):
            st2, _, body2 = _fetch(f"https://jobs.lever.co/{site}/{jid}")
            if st2 in (404, 410) or CLOSED_RE.search(body2):
                return DEAD, "lever_api_404+page"
            return UNCERTAIN, "lever_api404_page200"
        return _classify_body(st, final, body, meta=meta)
    return _classify_body(*_fetch(url), meta=meta, requested_url=url)


def _ashby(url: str, *, meta: dict[str, str | None] | None = None) -> tuple[str, str]:
    p = urllib.parse.urlparse(url)
    parts = [x for x in p.path.split("/") if x]
    if len(parts) >= 2:
        board, jid = parts[0], parts[1]
        st, final, body = _fetch(
            f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false",
            accept="application/json")
        if st == 200:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return UNCERTAIN, "ashby_api_invalid_json"
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                return UNCERTAIN, "ashby_api_invalid_payload"
            jobs = payload["jobs"]
            if any(not isinstance(job, dict) for job in jobs):
                return UNCERTAIN, "ashby_api_invalid_payload"
            ids = {str(job.get("id")) for job in jobs if job.get("id") is not None}
            if jid in ids:
                return LIVE, "ashby_api_listed"
            st2, _, body2 = _fetch(url)
            if st2 in (404, 410) or CLOSED_RE.search(body2):
                return DEAD, "ashby_absent+page_gone"
            return UNCERTAIN, "ashby_absent_page200"
        if st in (404, 410):
            st2, _, body2 = _fetch(url)
            if st2 in (404, 410) or CLOSED_RE.search(body2):
                return DEAD, "ashby_board404+page_gone"
            return UNCERTAIN, "ashby_board404_page200"
        return _classify_body(st, final, body, meta=meta)
    return _classify_body(*_fetch(url), meta=meta, requested_url=url)


def _workday(url: str, *, meta: dict[str, str | None] | None = None) -> tuple[str, str]:
    p = urllib.parse.urlparse(url)
    host = host_of(url)
    tenant = host.split(".")[0]
    parts = [x for x in p.path.split("/") if x]
    if "job" in parts:
        i = parts.index("job")
        site = parts[i - 1] if i >= 1 else None
        rest = parts[i + 1:]
        if site and rest:
            cxs = f"{p.scheme}://{host}/wday/cxs/{tenant}/{site}/job/" + "/".join(rest)
            st, _, body = _fetch(cxs, accept="application/json")
            if st == 200:
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    return UNCERTAIN, "workday_cxs_invalid_json"
                if not isinstance(payload, dict) or not isinstance(payload.get("jobPostingInfo"), dict):
                    return UNCERTAIN, "workday_cxs_invalid_payload"
                return LIVE, "workday_cxs_200"
            if st in (404, 410):
                return DEAD, "workday_cxs_404"
            if st in (401, 403, 429):
                # Some Workday tenants block anonymous CXS JSON while serving the
                # public posting page.  Keep the gate read-only and require both
                # a healthy page response and recognizable Workday job evidence.
                page_status, page_url, page_body = _fetch(url)
                if page_status in (404, 410):
                    return DEAD, f"workday_page_{page_status}_after_cxs_{st}"
                if page_status == 200:
                    normalized = _visible_text(page_body).casefold()
                    has_title_marker = bool(re.search(
                        r"data-automation-id\s*=\s*['\"]jobtitleheading['\"]",
                        page_body,
                        re.I,
                    ))
                    has_apply_control = bool(re.search(r"\bapply\b", normalized))
                    page_state, _page_reason = _classify_body(
                        page_status, page_url, page_body, meta=meta, requested_url=url
                    )
                    if page_state == DEAD:
                        return DEAD, f"workday_page_closed_after_cxs_{st}"
                    if page_state == LIVE and has_title_marker and has_apply_control:
                        return LIVE, f"workday_page_200_after_cxs_{st}"
                return UNCERTAIN, f"workday_blocked_{st}"
            return UNCERTAIN, f"workday_cxs_{st}"
    return UNCERTAIN, "workday_unparsed"


def _dispatch(url: str, *, meta: dict[str, str | None] | None = None) -> tuple[str, str]:
    host = host_of(url)
    if base_host(host) in BLOCKED_HOSTS:
        return UNCERTAIN, "blocked_host_policy"
    if (host in ("boards.greenhouse.io", "job-boards.greenhouse.io", "grnh.se")
            or "gh_jid" in (urllib.parse.urlparse(url).query or "")):
        return _greenhouse(url, meta=meta)
    if host == "jobs.lever.co":
        return _lever(url, meta=meta)
    if host == "jobs.ashbyhq.com":
        return _ashby(url, meta=meta)
    if host.endswith("myworkdayjobs.com"):
        return _workday(url, meta=meta)
    return _classify_body(*_fetch(url), meta=meta, requested_url=url)


def probe_url(url: str, *, meta: dict[str, str | None] | None = None) -> tuple[str, str]:
    """Return (status, reason) for a single posting URL. Never raises.

    status is one of "live" / "dead" / "uncertain". Read-only: issues only
    GET requests / public JSON-API reads and never submits anything.
    """
    if not url or not url.startswith(("http://", "https://")):
        return UNCERTAIN, "no_http_url"
    try:
        return _dispatch(url, meta=meta)
    except urllib.error.URLError as e:
        return UNCERTAIN, f"neterr:{getattr(e, 'reason', e)}"[:80]
    except Exception as e:  # never let one URL break a batch
        return UNCERTAIN, f"error:{type(e).__name__}"


def is_recent(iso_ts: str | None, max_age_days: int) -> bool:
    """True if iso_ts is within max_age_days of now (used to skip fresh verdicts)."""
    if not iso_ts:
        return False
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days < max_age_days
    except Exception:
        return False


def _row_get(row, key: str):
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _effective_url(row) -> str:
    app = str(_row_get(row, "application_url") or "")
    if app.startswith(("http://", "https://")):
        return app
    return str(_row_get(row, "url") or "")


def _write_liveness_results(conn, results: list[tuple[str, str, str, dict[str, str | None]]], *,
                            dry_run: bool = False) -> int:
    wrote = 0
    now = datetime.now(timezone.utc).isoformat()
    if dry_run:
        return wrote
    for i, (url, status, reason, meta) in enumerate(results):
        transient = reason.startswith(("neterr", "error"))
        expected_url = meta.get("_probed_url") if meta else None
        url_guard = (
            " AND (CASE WHEN application_url LIKE 'http%' THEN application_url ELSE url END) = ?"
            if expected_url else ""
        )
        if transient:
            cursor = conn.execute(
                "UPDATE jobs SET liveness_status = ?, liveness_reason = ? WHERE url = ?" + url_guard,
                (status, reason, url, *([expected_url] if expected_url else [])))
        else:
            cursor = conn.execute(
                "UPDATE jobs SET liveness_status = ?, liveness_reason = ?, "
                "posted_at = COALESCE(posted_at, ?), "
                "valid_through = COALESCE(valid_through, ?), "
                "last_verified_live = ? WHERE url = ?" + url_guard,
                (
                    status, reason,
                    (meta.get("posted_at") if meta else None),
                    (meta.get("valid_through") if meta else None),
                    now,
                    url,
                    *([expected_url] if expected_url else []),
                ))
        wrote += max(cursor.rowcount, 0)
        if i % 200 == 0:
            conn.commit()
    conn.commit()
    return wrote


def verify_candidate_rows(conn, rows, *, max_age_days: int = 1, workers: int = 16,
                          dry_run: bool = False) -> dict:
    """Verify exact candidate rows before token-spending stages run."""
    row_list = list(rows)
    todo: list[tuple[str, str, dict[str, str | None]]] = []
    skipped_fresh = 0
    for row in row_list:
        if max_age_days > 0 and is_recent(_row_get(row, "last_verified_live"), max_age_days):
            skipped_fresh += 1
            continue
        effective = _effective_url(row)
        if not effective.startswith(("http://", "https://")):
            continue
        todo.append((str(_row_get(row, "url") or ""), effective, {}))

    results: list[tuple[str, str, str, dict[str, str | None]]] = []
    if todo:
        worker_count = max(1, int(workers or 1))
        with ThreadPoolExecutor(max_workers=worker_count) as ex:
            futs = {
                ex.submit(probe_url, app, meta=meta): (url, app, meta)
                for (url, app, meta) in todo
            }
            for fut in as_completed(futs):
                url, app, meta = futs[fut]
                status, reason = fut.result()
                meta["_probed_url"] = app
                results.append((url, status, reason, meta))

    counts = Counter(s for _, s, _, _ in results)
    wrote = _write_liveness_results(conn, results, dry_run=dry_run)
    return {"checked": len(results), "by_status": dict(counts),
            "skipped_fresh": skipped_fresh, "candidates": len(row_list), "wrote": wrote}


def verify_jobs(conn, *, tiers=("priority", "recommended"), score_floor: float | None = None,
                max_age_days: int = 7, limit: int = 0, workers: int = 16, dry_run: bool = False,
                progress=None) -> dict:
    """Batch-verify posting liveness and stamp the liveness_* columns.

    READ-ONLY toward employers; the only DB writes are UPDATEs to
    liveness_status / liveness_reason / last_verified_live. NEVER deletes a row,
    so dead postings stay in the DB (with their full JD/scores) for training.

    Args:
        conn:        an open sqlite3 connection to the jobs DB.
        tiers:       audit_label values to verify.
        max_age_days: skip jobs already verified within this many days (0 = all).
        limit:       cap the number of jobs probed (0 = no cap).
        workers:     concurrent probe threads (per-host throttling still applies).
        dry_run:     probe + report but write nothing.

    Returns a summary dict: {checked, by_status, skipped_fresh, candidates, wrote}.
    """
    placeholders = ",".join("?" * len(tiers))
    floor_clause = ""
    params: list[object] = list(tiers)
    if score_floor is not None:
        floor_clause = " OR COALESCE(audit_score, fit_score) >= ?"
        params.append(score_floor)

    rows = conn.execute(
        f"""SELECT url, application_url, liveness_status, last_verified_live
             FROM jobs
            WHERE (audit_label IN ({placeholders}){floor_clause})
              AND duplicate_of_url IS NULL
              AND (application_url LIKE 'http%' OR url LIKE 'http%')
            ORDER BY (last_verified_live IS NOT NULL), last_verified_live""",
        params,
    ).fetchall()

    todo: list[tuple[str, str, dict[str, str | None]]] = []
    skipped_fresh = 0
    for r in rows:
        if max_age_days > 0 and is_recent(r["last_verified_live"], max_age_days):
            skipped_fresh += 1
            continue
        # Probe the EFFECTIVE apply target — mirror acquire_job's
        # `application_url or url` fallback so jobs whose only usable link is in
        # `url` (e.g. linkedin.com/jobs/view/<id>, talent.com/view?id=...) are
        # covered too, not silently left unchecked.
        app = r["application_url"]
        effective = app if (app or "").startswith(("http://", "https://")) else r["url"]
        todo.append((r["url"], effective, {}))
    if limit > 0:
        todo = todo[:limit]

    results: list[tuple[str, str, str, dict[str, str | None]]] = []
    worker_count = max(1, int(workers or 1))
    with ThreadPoolExecutor(max_workers=worker_count) as ex:
        futs = {
            ex.submit(probe_url, app, meta=meta): (url, app, meta)
            for (url, app, meta) in todo
        }
        done = 0
        for fut in as_completed(futs):
            url, app, meta = futs[fut]
            status, reason = fut.result()
            meta["_probed_url"] = app
            results.append((url, status, reason, meta))
            done += 1
            if progress and (done % 50 == 0 or done == len(todo)):
                progress(done, len(todo), results)

    counts = Counter(s for _, s, _, _ in results)
    wrote = _write_liveness_results(conn, results, dry_run=dry_run)

    return {"checked": len(results), "by_status": dict(counts),
            "skipped_fresh": skipped_fresh, "candidates": len(rows), "wrote": wrote}
