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


def _classify_body(status: int, final_url: str, body: str) -> tuple[str, str]:
    if status in (404, 410):
        return DEAD, f"http_{status}"
    if status in (401, 403, 429, 999):
        return UNCERTAIN, f"blocked_{status}"
    if status >= 500:
        return UNCERTAIN, f"server_{status}"
    if status != 200:
        return UNCERTAIN, f"http_{status}"
    m = CLOSED_RE.search(body)
    if m:
        return DEAD, f"text:{m.group(0)[:40]!r}"
    vt = VALID_THROUGH_RE.search(body)
    if vt:
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
    return LIVE, "ok_200"


def _greenhouse(url: str) -> tuple[str, str]:
    host = host_of(url)
    if host == "grnh.se":
        st, final, body = _fetch(url)
        if host_of(final) != "grnh.se":
            return _dispatch(final)
        return _classify_body(st, final, body)
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
        return _classify_body(*_fetch(url))
    if token and job_id:
        api = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"
        st, final, body = _fetch(api, accept="application/json")
        if st == 200 and '"id"' in body:
            return LIVE, "gh_api_200"
        if st in (404, 410):
            stl, _, _ = _fetch(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                               accept="application/json")
            if stl == 200:
                return DEAD, "gh_api_404"
            return UNCERTAIN, "gh_token_unconfirmed"
        return _classify_body(st, final, body)
    return _classify_body(*_fetch(url))


def _lever(url: str) -> tuple[str, str]:
    p = urllib.parse.urlparse(url)
    parts = [x for x in p.path.split("/") if x and x != "apply"]
    if len(parts) >= 2:
        site, jid = parts[0], parts[1]
        st, final, body = _fetch(f"https://api.lever.co/v0/postings/{site}/{jid}",
                                 accept="application/json")
        if st == 200 and '"id"' in body:
            return LIVE, "lever_api_200"
        if st in (404, 410):
            st2, _, body2 = _fetch(f"https://jobs.lever.co/{site}/{jid}")
            if st2 in (404, 410) or CLOSED_RE.search(body2):
                return DEAD, "lever_api_404+page"
            return UNCERTAIN, "lever_api404_page200"
        return _classify_body(st, final, body)
    return _classify_body(*_fetch(url))


def _ashby(url: str) -> tuple[str, str]:
    p = urllib.parse.urlparse(url)
    parts = [x for x in p.path.split("/") if x]
    if len(parts) >= 2:
        board, jid = parts[0], parts[1]
        st, final, body = _fetch(
            f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false",
            accept="application/json")
        if st == 200:
            try:
                ids = {str(j.get("id")) for j in json.loads(body).get("jobs", [])}
                if jid in ids:
                    return LIVE, "ashby_api_listed"
            except Exception:
                pass
            st2, _, body2 = _fetch(url)
            if st2 in (404, 410) or CLOSED_RE.search(body2):
                return DEAD, "ashby_absent+page_gone"
            return UNCERTAIN, "ashby_absent_page200"
        if st in (404, 410):
            st2, _, body2 = _fetch(url)
            if st2 in (404, 410) or CLOSED_RE.search(body2):
                return DEAD, "ashby_board404+page_gone"
            return UNCERTAIN, "ashby_board404_page200"
        return _classify_body(st, final, body)
    return _classify_body(*_fetch(url))


def _workday(url: str) -> tuple[str, str]:
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
            if st == 200 and "jobPostingInfo" in body:
                return LIVE, "workday_cxs_200"
            if st in (404, 410):
                return DEAD, "workday_cxs_404"
            if st in (401, 403, 429):
                return UNCERTAIN, f"workday_blocked_{st}"
            return UNCERTAIN, f"workday_cxs_{st}"
    return UNCERTAIN, "workday_unparsed"


def _dispatch(url: str) -> tuple[str, str]:
    host = host_of(url)
    if base_host(host) in BLOCKED_HOSTS:
        return UNCERTAIN, "blocked_host_policy"
    if (host in ("boards.greenhouse.io", "job-boards.greenhouse.io", "grnh.se")
            or "gh_jid" in (urllib.parse.urlparse(url).query or "")):
        return _greenhouse(url)
    if host == "jobs.lever.co":
        return _lever(url)
    if host == "jobs.ashbyhq.com":
        return _ashby(url)
    if host.endswith("myworkdayjobs.com"):
        return _workday(url)
    return _classify_body(*_fetch(url))


def probe_url(url: str) -> tuple[str, str]:
    """Return (status, reason) for a single posting URL. Never raises.

    status is one of "live" / "dead" / "uncertain". Read-only: issues only
    GET requests / public JSON-API reads and never submits anything.
    """
    if not url or not url.startswith(("http://", "https://")):
        return UNCERTAIN, "no_http_url"
    try:
        return _dispatch(url)
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


def verify_jobs(conn, *, tiers=("priority", "recommended"), max_age_days: int = 7,
                limit: int = 0, workers: int = 16, dry_run: bool = False,
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
    rows = conn.execute(
        f"""SELECT url, application_url, liveness_status, last_verified_live
              FROM jobs
             WHERE audit_label IN ({placeholders})
               AND application_url LIKE 'http%'
               AND duplicate_of_url IS NULL""",
        list(tiers),
    ).fetchall()

    todo = []
    skipped_fresh = 0
    for r in rows:
        if max_age_days > 0 and is_recent(r["last_verified_live"], max_age_days):
            skipped_fresh += 1
            continue
        todo.append((r["url"], r["application_url"]))
    if limit > 0:
        todo = todo[:limit]

    results: list[tuple[str, str, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(probe_url, app): (url, app) for (url, app) in todo}
        done = 0
        for fut in as_completed(futs):
            url, _app = futs[fut]
            status, reason = fut.result()
            results.append((url, status, reason))
            done += 1
            if progress and (done % 50 == 0 or done == len(todo)):
                progress(done, len(todo), results)

    counts = Counter(s for _, s, _ in results)
    wrote = 0
    if not dry_run:
        now = datetime.now(timezone.utc).isoformat()
        for i, (url, status, reason) in enumerate(results):
            # Transient fetch failures: record the status but leave
            # last_verified_live untouched so the job is re-checked next run.
            transient = reason.startswith(("neterr", "error"))
            if transient:
                conn.execute(
                    "UPDATE jobs SET liveness_status = ?, liveness_reason = ? WHERE url = ?",
                    (status, reason, url))
            else:
                conn.execute(
                    "UPDATE jobs SET liveness_status = ?, liveness_reason = ?, "
                    "last_verified_live = ? WHERE url = ?",
                    (status, reason, now, url))
            wrote += 1
            if i % 200 == 0:
                conn.commit()
        conn.commit()

    return {"checked": len(results), "by_status": dict(counts),
            "skipped_fresh": skipped_fresh, "candidates": len(rows), "wrote": wrote}
