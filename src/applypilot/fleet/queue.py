"""Governor-aware atomic claims for the v3 queues (apply / compute / search / linkedin).

Builds on the proven ``apply.pgqueue`` lease pattern (FOR UPDATE SKIP LOCKED) but
gates the apply claim on the outcome-aware governor (R6), the per-IP breaker, the
owner approval gate (R11), and the cross-board dedup set (R9). Compute is cost-cap
gated (R14); search is board-scrape governed + RECURRING (RF3); LinkedIn is the
single-account mutex (R1).
"""
from __future__ import annotations

import json

from applypilot.fleet import dedup as _dedup
from applypilot.fleet import governor

# ---------------------------------------------------------------------------
# APPLY lease -- governed, approval-gated, dedup-guarded (R6, R9, R11).
# ---------------------------------------------------------------------------
_LEASE_APPLY = """
WITH home AS (SELECT count_24h, daily_cap, breaker_state FROM rate_governor WHERE scope_key = %(home_scope)s),
     glob AS (SELECT count_24h, daily_cap FROM rate_governor WHERE scope_key = 'global'),
     next_job AS (
       SELECT q.url
       FROM apply_queue q
       LEFT JOIN rate_governor g ON g.scope_key = 'host:' || COALESCE(q.target_host, q.apply_domain)
       LEFT JOIN home ON TRUE
       LEFT JOIN glob ON TRUE
       WHERE q.status = 'queued' AND q.lane = 'ats' AND q.approved_batch IS NOT NULL
         AND (glob.count_24h IS NULL OR glob.count_24h < glob.daily_cap)
         AND COALESCE(home.breaker_state, 'ok') = 'ok'
         AND (home.count_24h IS NULL OR home.count_24h < home.daily_cap)
         AND COALESCE(g.breaker_state, 'ok') = 'ok'
         AND COALESCE(g.count_24h, 0) < COALESCE(g.daily_cap, 2000000000)
         AND (g.last_applied_at IS NULL
              OR g.last_applied_at < now() - make_interval(secs => COALESCE(g.min_gap_seconds, 90) * (0.7 + random()*0.7)))
         AND NOT EXISTS (SELECT 1 FROM applied_set a WHERE a.dedup_key = q.dedup_key)
       ORDER BY q.score DESC, q.url
       LIMIT 1
       FOR UPDATE OF q SKIP LOCKED
     )
UPDATE apply_queue q
SET status='leased', lease_owner=%(worker)s, lease_expires_at = now() + make_interval(secs => %(ttl)s),
    last_attempted_at = now(), attempts = q.attempts + 1, updated_at = now(), worker_home_ip = %(home_ip)s
FROM next_job WHERE q.url = next_job.url
RETURNING q.url, q.company, q.title, q.application_url,
          COALESCE(q.target_host, q.apply_domain) AS target_host, q.score, q.dedup_key, q.attempts;
"""


def lease_apply(conn, worker_id, *, home_ip, ttl_seconds=1200):
    with conn.cursor() as cur:
        cur.execute(_LEASE_APPLY, {
            "worker": worker_id, "home_ip": home_ip,
            "home_scope": governor.home_ip_scope(home_ip), "ttl": ttl_seconds,
        })
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


def write_apply_result(conn, worker_id, url, *, status, target_host, home_ip,
                       apply_status=None, apply_error=None, est_cost_usd=0, outcome=None):
    """Close the apply (lease-owner guarded), record the governor outcome on
    global+host+home_ip (bump cap on a confirmed apply), and UPSERT applied_set
    so the posting can never be applied to again. One transaction.

    ``outcome`` in {'success','captcha','block'}; derived from ``status`` if None.
    Returns False if the lease was lost (already reclaimed/closed)."""
    if outcome is None:
        outcome = {"applied": "success", "blocked": "block"}.get(status, "captcha")
    scopes = [governor.GLOBAL, governor.host_scope(target_host), governor.home_ip_scope(home_ip)]
    col = governor._OUTCOME_COL[outcome]
    extra = ", count_24h = count_24h + 1, last_applied_at = now()" if status == "applied" else ""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE apply_queue SET status=%s, apply_status=%s, apply_error=%s, est_cost_usd=COALESCE(%s,0), "
            "applied_at = CASE WHEN %s = 'applied' THEN now() ELSE applied_at END, worker_id=%s, updated_at=now() "
            "WHERE url=%s AND lease_owner=%s",
            (status, apply_status, apply_error, est_cost_usd, status, worker_id, url, worker_id),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return False
        for sk in scopes:
            cur.execute("INSERT INTO rate_governor (scope_key) VALUES (%s) ON CONFLICT (scope_key) DO NOTHING", (sk,))
            cur.execute(f"UPDATE rate_governor SET {col} = {col} + 1{extra}, updated_at = now() WHERE scope_key = %s", (sk,))
        if status == "applied":
            cur.execute(
                "INSERT INTO applied_set (dedup_key, company, applied_url) "
                "SELECT dedup_key, company, application_url FROM apply_queue WHERE url=%s AND dedup_key IS NOT NULL "
                "ON CONFLICT (dedup_key) DO NOTHING",
                (url,),
            )
    conn.commit()
    return True


def push_apply_jobs(conn, rows, *, approved_batch=None, commit=True) -> int:
    """UPSERT apply_queue rows with the v3 columns (dedup_key, target_host, lane,
    approved_batch). Only refreshes ``queued`` rows. ``rows`` need url, company,
    title, application_url, score, and target_host (or apply_domain)."""
    n = 0
    with conn.cursor() as cur:
        for r in rows:
            host = r.get("target_host") or r.get("apply_domain")
            dk = r.get("dedup_key") or _dedup.dedup_key(r.get("company"), r.get("title"))
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, apply_domain, target_host, lane, dedup_key, approved_batch) "
                "VALUES (%(url)s,%(company)s,%(title)s,%(application_url)s,%(score)s,%(host)s,%(host)s,'ats',%(dk)s,%(batch)s) "
                "ON CONFLICT (url) DO UPDATE SET company=EXCLUDED.company, title=EXCLUDED.title, "
                "application_url=EXCLUDED.application_url, score=EXCLUDED.score, target_host=EXCLUDED.target_host, "
                "dedup_key=EXCLUDED.dedup_key, "
                "approved_batch=COALESCE(EXCLUDED.approved_batch, apply_queue.approved_batch), updated_at=now() "
                "WHERE apply_queue.status='queued'",
                {**r, "host": host, "dk": dk, "batch": approved_batch},
            )
            n += cur.rowcount
    if commit:
        conn.commit()
    return n


def approve_jobs(conn, urls, batch, *, commit=True) -> int:
    """Stamp the owner approval token on queued rows (R11 gray-zone / batch approve)."""
    with conn.cursor() as cur:
        cur.execute("UPDATE apply_queue SET approved_batch=%s, updated_at=now() "
                    "WHERE url = ANY(%s) AND status='queued'", (batch, list(urls)))
        n = cur.rowcount
    if commit:
        conn.commit()
    return n


# ---------------------------------------------------------------------------
# COMPUTE lease -- cost-cap gated, no IP governor (§8, R14).
# ---------------------------------------------------------------------------
def _cost_cap_exceeded(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT cost_cap_daily_usd, cost_cap_total_usd FROM fleet_config WHERE id=1")
        cfg = cur.fetchone()
        if not cfg:
            return False
        daily = float(cfg["cost_cap_daily_usd"] or 0)
        total = float(cfg["cost_cap_total_usd"] or 0)
        if daily > 0:
            cur.execute("SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_usage WHERE ts >= now() - interval '24 hours'")
            if float(cur.fetchone()["s"]) >= daily:
                return True
        if total > 0:
            cur.execute("SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_usage")
            if float(cur.fetchone()["s"]) >= total:
                return True
    return False


_LEASE_COMPUTE = """
WITH next AS (
  SELECT url FROM compute_queue WHERE status='queued'
  ORDER BY updated_at LIMIT 1 FOR UPDATE SKIP LOCKED
)
UPDATE compute_queue c SET status='leased', lease_owner=%(worker)s,
  lease_expires_at = now() + make_interval(secs => %(ttl)s), attempts = c.attempts + 1, updated_at = now()
FROM next WHERE c.url = next.url
RETURNING c.url, c.task, c.payload, c.attempts;
"""


def lease_compute(conn, worker_id, *, ttl_seconds=1200):
    if _cost_cap_exceeded(conn):
        return None
    with conn.cursor() as cur:
        cur.execute(_LEASE_COMPUTE, {"worker": worker_id, "ttl": ttl_seconds})
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


def write_compute_result(conn, worker_id, url, *, result, status="done", cost_usd=0,
                         model=None, task=None, machine_owner=None, tokens_in=None, tokens_out=None):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE compute_queue SET status=%s, result=%s, est_cost_usd=COALESCE(%s,0), updated_at=now() "
            "WHERE url=%s AND lease_owner=%s",
            (status, json.dumps(result) if result is not None else None, cost_usd, url, worker_id),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return False
        cur.execute(
            "INSERT INTO llm_usage (worker_id, machine_owner, task, model, tokens_in, tokens_out, cost_usd) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (worker_id, machine_owner, task, model, tokens_in, tokens_out, cost_usd),
        )
    conn.commit()
    return True


def push_compute_jobs(conn, rows, *, commit=True) -> int:
    """rows: url, task, payload(dict), est_cost_usd."""
    n = 0
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(
                "INSERT INTO compute_queue (url, task, payload, est_cost_usd) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (url) DO UPDATE SET task=EXCLUDED.task, payload=EXCLUDED.payload, updated_at=now() "
                "WHERE compute_queue.status='queued'",
                (r["url"], r["task"], json.dumps(r.get("payload")) if r.get("payload") is not None else None,
                 r.get("est_cost_usd", 0)),
            )
            n += cur.rowcount
    if commit:
        conn.commit()
    return n


# ---------------------------------------------------------------------------
# SEARCH lease -- RECURRING, board-scrape governed (RF3, RF2).
# ---------------------------------------------------------------------------
_LEASE_SEARCH = """
WITH next AS (
  SELECT s.task_id FROM search_tasks s
  LEFT JOIN rate_governor g ON g.scope_key = 'board:' || s.board
  WHERE s.status='queued' AND s.enabled AND s.next_due_at <= now()
    AND COALESCE(g.breaker_state, 'ok') = 'ok'
    AND COALESCE(g.count_24h, 0) < COALESCE(g.daily_cap, 2000000000)
    AND (g.last_applied_at IS NULL
         OR g.last_applied_at < now() - make_interval(secs => COALESCE(g.min_gap_seconds, 90) * (0.7 + random()*0.7)))
  ORDER BY s.next_due_at LIMIT 1 FOR UPDATE OF s SKIP LOCKED
)
UPDATE search_tasks s SET status='leased', lease_owner=%(worker)s,
  lease_expires_at = now() + make_interval(secs => %(ttl)s), attempts = s.attempts + 1, updated_at = now()
FROM next WHERE s.task_id = next.task_id
RETURNING s.task_id, s.query, s.board, s.location, s.params, s.cadence_seconds;
"""


def lease_search(conn, worker_id, *, ttl_seconds=900):
    with conn.cursor() as cur:
        cur.execute(_LEASE_SEARCH, {"worker": worker_id, "ttl": ttl_seconds})
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


def complete_search(conn, worker_id, task_id, *, result_count=0, board=None, error=None, cadence_seconds=None):
    """Mark the search done, record a scrape outcome on the board scope, and
    RE-SCHEDULE the task (status back to 'queued', next_due_at = now + cadence)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE search_tasks SET status='queued', last_run_at=now(), result_count=%s, last_error=%s, "
            "next_due_at = now() + make_interval(secs => COALESCE(%s, cadence_seconds)), updated_at=now() "
            "WHERE task_id=%s AND lease_owner=%s",
            (result_count, error, cadence_seconds, task_id, worker_id),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return False
        if board:
            outcome = "block" if error == "blocked" else ("captcha" if error == "captcha" else "success")
            col = governor._OUTCOME_COL[outcome]
            extra = ", count_24h = count_24h + 1, last_applied_at = now()" if outcome == "success" else ""
            sk = governor.board_scope(board)
            cur.execute("INSERT INTO rate_governor (scope_key) VALUES (%s) ON CONFLICT (scope_key) DO NOTHING", (sk,))
            cur.execute(f"UPDATE rate_governor SET {col} = {col} + 1{extra}, updated_at=now() WHERE scope_key=%s", (sk,))
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# LINKEDIN lease -- single-account mutex, owner-IP only (R1).
# ---------------------------------------------------------------------------
_LEASE_LINKEDIN = """
WITH acct AS (SELECT count_24h, daily_cap, last_applied_at, min_gap_seconds, breaker_state
              FROM rate_governor WHERE scope_key = 'account:linkedin'),
     next AS (
       SELECT q.url FROM linkedin_queue q LEFT JOIN acct a ON TRUE
       WHERE q.status='queued' AND q.approved_batch IS NOT NULL
         AND (a.count_24h IS NULL OR a.count_24h < a.daily_cap)
         AND COALESCE(a.breaker_state, 'ok') = 'ok'
         AND (a.last_applied_at IS NULL OR a.last_applied_at < now() - make_interval(secs => COALESCE(a.min_gap_seconds, 300)))
       ORDER BY q.score DESC, q.url LIMIT 1 FOR UPDATE OF q SKIP LOCKED
     )
UPDATE linkedin_queue q SET status='leased', lease_owner=%(worker)s,
  lease_expires_at = now() + make_interval(secs => %(ttl)s), last_attempted_at=now(), attempts=q.attempts+1, updated_at=now()
FROM next WHERE q.url = next.url
RETURNING q.url, q.company, q.title, q.application_url, q.score;
"""


def lease_linkedin(conn, worker_id, *, public_ip, owner_ip, ttl_seconds=1200):
    """LinkedIn lease: ONLY from the one owner IP, serialized by the account mutex."""
    if public_ip != owner_ip:
        return None  # broker-level guard: LinkedIn never from a different IP
    with conn.cursor() as cur:
        cur.execute(_LEASE_LINKEDIN, {"worker": worker_id, "ttl": ttl_seconds})
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Reclaim (crash-safety) for the compute + search queues. (apply_queue reclaim
# is provided by apply.pgqueue.reclaim_stale_leases.)
# ---------------------------------------------------------------------------
def reclaim_compute(conn, *, grace_seconds=30, commit=True) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE compute_queue SET status='queued', lease_owner=NULL, lease_expires_at=NULL, updated_at=now() "
            "WHERE status='leased' AND lease_expires_at < now() - make_interval(secs => %s) RETURNING url",
            (grace_seconds,),
        )
        n = len(cur.fetchall())
    if commit:
        conn.commit()
    return n


def reclaim_search(conn, *, grace_seconds=30, commit=True) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE search_tasks SET status='queued', lease_owner=NULL, lease_expires_at=NULL, updated_at=now() "
            "WHERE status='leased' AND lease_expires_at < now() - make_interval(secs => %s) RETURNING task_id",
            (grace_seconds,),
        )
        n = len(cur.fetchall())
    if commit:
        conn.commit()
    return n
