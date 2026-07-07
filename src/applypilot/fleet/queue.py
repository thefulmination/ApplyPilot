"""Governor-aware atomic claims for the v3 queues (apply / compute / search / linkedin).

Builds on the proven ``apply.pgqueue`` lease pattern (FOR UPDATE SKIP LOCKED) but
gates the apply claim on the outcome-aware governor (R6), the per-IP breaker, the
owner approval gate (R11), and the cross-board dedup set (R9). Compute is cost-cap
gated (R14); search is board-scrape governed + RECURRING (RF3); LinkedIn is the
single-account mutex (R1).
"""
from __future__ import annotations

import json
import math
import numbers
import urllib.parse as _urlparse

from applypilot import config
from applypilot.fleet import dedup as _dedup
from applypilot.fleet import governor

# ---------------------------------------------------------------------------
# APPLY lease -- governed, approval-gated, dedup-guarded (R6, R9, R11).
#
# Atomicity notes (catastrophe gate):
#   - cfg … FOR UPDATE row-locks the single fleet_config row, serializing ALL
#     apply leases on it — exactly as _LEASE_LINKEDIN locks 'account:linkedin'.
#     No two transactions can pass the canary guard simultaneously.
#   - The reserve UPDATE is a data-modifying CTE. Postgres runs ALL WITH
#     data-modifying CTEs to completion even when unreferenced in the outer
#     statement. It only fires when EXISTS(next_job), so a no-op lease (empty
#     queue / all guards fail) never decrements canary_remaining.
#   - paused = (paused OR canary_remaining-1 <= 0) only ever SETS paused to
#     TRUE; it never clears it, so a cost-pause or manual pause is preserved.
# ---------------------------------------------------------------------------
_LEASE_APPLY = """
WITH cfg AS (SELECT canary_enabled, canary_remaining, paused, ats_paused, spend_cap_usd FROM fleet_config WHERE id=1 FOR UPDATE),
     home AS (SELECT count_24h, daily_cap, breaker_state, breaker_until FROM rate_governor WHERE scope_key = %(home_scope)s),
     glob AS (SELECT count_24h, daily_cap FROM rate_governor WHERE scope_key = 'global'),
     next_job AS (
       SELECT q.url
       FROM apply_queue q
       LEFT JOIN rate_governor g ON g.scope_key = 'host:' || COALESCE(q.target_host, q.apply_domain)
       LEFT JOIN home ON TRUE
       LEFT JOIN glob ON TRUE
       LEFT JOIN cfg ON TRUE
       WHERE q.status = 'queued' AND q.lane = 'ats' AND q.approved_batch IS NOT NULL
         AND NOT COALESCE(cfg.paused, FALSE)
         -- H1: the Fleet Doctor's lane-pause writes ats_paused (NEVER the shared paused flag),
         -- honored here on the ATS lane only. _LEASE_LINKEDIN never reads ats_paused, so a
         -- Doctor pause can never halt the LinkedIn catastrophe lane.
         AND NOT COALESCE(cfg.ats_paused, FALSE)
         AND (NOT COALESCE(cfg.canary_enabled, FALSE) OR cfg.canary_remaining > 0)
         AND (COALESCE(cfg.spend_cap_usd, 0) <= 0
              OR (SELECT COALESCE(SUM(est_cost_usd), 0) FROM apply_queue) < cfg.spend_cap_usd)
         AND (glob.count_24h IS NULL OR glob.count_24h < glob.daily_cap)
         AND COALESCE(home.breaker_state, 'ok') != 'demoted'
         AND COALESCE(NOT (home.breaker_state = 'paused' AND COALESCE(home.breaker_until, 'infinity'::timestamptz) >= now()), TRUE)
         AND (home.count_24h IS NULL OR home.count_24h < home.daily_cap)
         AND COALESCE(g.breaker_state, 'ok') != 'demoted'
         AND COALESCE(NOT (g.breaker_state = 'paused' AND COALESCE(g.breaker_until, 'infinity'::timestamptz) >= now()), TRUE)
         AND COALESCE(g.count_24h, 0) < COALESCE(g.daily_cap, 2000000000)
         -- H6: the Doctor's host_skip is a self-expiring lease FILTER (doctor_skip_until),
         -- NOT an approved_batch=NULL un-approve -- so vetted owner approval is preserved and
         -- the skip auto-reverts at its TTL exactly like the breaker's breaker_until.
         AND COALESCE(g.doctor_skip_until, '-infinity'::timestamptz) < now()
         AND NOT (
           LOWER(TRIM(COALESCE(q.company,''))) = ANY(%(blocked_names)s)
           OR q.url ILIKE ANY(%(blocked_pats)s)
           OR COALESCE(q.application_url,'') ILIKE ANY(%(blocked_pats)s)
         )
         -- H4: the effective min-gap is GREATEST(breaker-owned min_gap, Doctor-owned floor), so
         -- a Doctor pace is monotone-by-construction and the watchdog breaker's min_gap restore
         -- can never silently wipe a still-active Doctor pace (and vice-versa).
         -- A3: gate off COALESCE(last_applied_at, last_attempt_at). last_applied_at is stamped ONLY
         -- on a confirmed apply, so a never-succeeded (hard-blocking) host had last_applied_at=NULL
         -- forever and short-circuited the WHOLE gap (both the breaker min_gap AND the Doctor floor),
         -- leasing back-to-back at zero spacing. last_attempt_at is now stamped on every outcome
         -- (success+captcha+block), so the GREATEST gap is actually enforced on those hosts.
         AND (COALESCE(g.last_applied_at, g.last_attempt_at) IS NULL
              OR COALESCE(g.last_applied_at, g.last_attempt_at) < now() - make_interval(secs =>
                   GREATEST(COALESCE(g.min_gap_seconds, 90), COALESCE(g.doctor_min_gap_floor, 0))
                   * (0.7 + random()*0.7)))
         AND NOT EXISTS (SELECT 1 FROM applied_set a WHERE a.dedup_key = q.dedup_key)
       ORDER BY q.score DESC, q.url
       LIMIT 1
       FOR UPDATE OF q SKIP LOCKED
     ),
     reserve AS (
       UPDATE fleet_config
          SET canary_remaining = canary_remaining - 1,
              paused = (paused OR canary_remaining - 1 <= 0)
        WHERE id = 1 AND canary_enabled AND EXISTS (SELECT 1 FROM next_job)
       RETURNING 1
     )
UPDATE apply_queue q
SET status='leased', lease_owner=%(worker)s, lease_expires_at = now() + make_interval(secs => %(ttl)s),
    last_attempted_at = now(), attempts = q.attempts + 1, updated_at = now(), worker_home_ip = %(home_ip)s
FROM next_job WHERE q.url = next_job.url
RETURNING q.url, q.company, q.title, q.application_url,
          COALESCE(q.target_host, q.apply_domain) AS target_host, q.score, q.dedup_key, q.attempts;
"""


def lease_apply(conn, worker_id, *, home_ip, ttl_seconds=1200):
    blocked_names, blocked_pats = config.load_blocked_companies()
    with conn.cursor() as cur:
        cur.execute(_LEASE_APPLY, {
            "worker": worker_id, "home_ip": home_ip,
            "home_scope": governor.home_ip_scope(home_ip), "ttl": ttl_seconds,
            "blocked_names": list(blocked_names), "blocked_pats": blocked_pats,
        })
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


_REQUEUE_APPLY_SQL = """
UPDATE apply_queue
SET status           = 'queued'::apply_queue_status,
    apply_error      = %(apply_error)s,
    -- Undo the lease-time attempt bump: a usage/quota wall hit on turn 1 PROVABLY never
    -- touched the page, so this lease did nothing. NOT pinned to 99 (that is
    -- crash_unconfirmed / reclaim). GREATEST guards against underflow.
    attempts         = GREATEST(attempts - 1, 0),
    lease_owner      = NULL,
    lease_expires_at = NULL,
    updated_at       = now()
WHERE url = %(url)s
  AND lease_owner = %(worker)s          -- only the lease holder may re-queue it
RETURNING url;
"""


def requeue_apply(conn, worker_id, url, *, apply_error=None) -> bool:
    """Return a leased apply job to 'queued' so it can be re-leased -- the RETRYABLE
    counterpart to write_apply_result. Use ONLY when the run provably never touched the
    form (an agent usage/quota wall before any browser tool call): re-queuing can NEVER
    double-submit. Lease-owner guarded, so a reclaimed/foreign row is never clobbered.
    Returns True only if this worker held the lease."""
    with conn.cursor() as cur:
        cur.execute(_REQUEUE_APPLY_SQL, {"apply_error": apply_error, "worker": worker_id, "url": url})
        landed = cur.fetchone() is not None
    conn.commit()
    return landed


def _result_line(status, apply_status=None, apply_error=None) -> str:
    if status == "applied":
        return "RESULT:APPLIED"
    token = apply_error or apply_status or status or "unknown"
    return f"RESULT:{token}"


def write_apply_result(conn, worker_id, url, *, status, target_host, home_ip,
                        apply_status=None, apply_error=None, est_cost_usd=0, outcome=None,
                        agent=None, agent_model=None, apply_duration_ms=None):
    """Close the apply (lease-owner guarded), record the governor outcome on
    global+host+home_ip (bump cap on a confirmed apply), and UPSERT applied_set
    so the posting can never be applied to again. One transaction.

    ``outcome`` in {'success','captcha','block'}; derived from ``status`` if None.
    A generic page/form failure derives to None and records NO challenge outcome,
    so it cannot pollute the captcha_24h leading indicator that drives the breaker.
    Returns False if the lease was lost (already reclaimed/closed)."""
    if outcome is None:
        # Only true terminal classes map to a governor outcome; unknown -> None.
        outcome = {"applied": "success", "blocked": "block", "captcha": "captcha"}.get(status)
    scopes = [governor.GLOBAL, governor.host_scope(target_host), governor.home_ip_scope(home_ip)]
    # A3: last_attempt_at stamped on EVERY recorded outcome (so a never-succeeded host is still
    # spaced by the lease's COALESCE(last_applied_at, last_attempt_at) gap); last_applied_at +
    # count_24h remain confirmed-apply only.
    extra = (", count_24h = count_24h + 1, last_applied_at = now(), last_attempt_at = now()"
             if status == "applied" else ", last_attempt_at = now()")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE apply_queue SET status=%s, apply_status=%s, apply_error=%s, est_cost_usd=COALESCE(%s,0), "
            "agent_model=%s, apply_duration_ms=%s, "
            "applied_at = CASE WHEN %s = 'applied' THEN now() ELSE applied_at END, worker_id=%s, updated_at=now() "
            "WHERE url=%s AND lease_owner=%s",
            (
                status, apply_status, apply_error, est_cost_usd,
                agent_model, apply_duration_ms,
                status, worker_id, url, worker_id,
            ),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return False
        if outcome is not None:
            col = governor._OUTCOME_COL[outcome]
            for sk in scopes:
                cur.execute("INSERT INTO rate_governor (scope_key) VALUES (%s) ON CONFLICT (scope_key) DO NOTHING", (sk,))
                cur.execute(f"UPDATE rate_governor SET {col} = {col} + 1{extra}, updated_at = now() WHERE scope_key = %s", (sk,))
        # A confirmed apply OR a possibly-submitted crash both enter applied_set, so the
        # cross-board dedup guard at lease time covers a posting that may already carry
        # the user's name -- never re-apply (and never re-push, see fleet.sync).
        if status in ("applied", "crash_unconfirmed"):
            cur.execute(
                "INSERT INTO applied_set (dedup_key, company, applied_url) "
                "SELECT dedup_key, company, application_url FROM apply_queue WHERE url=%s AND dedup_key IS NOT NULL "
                "ON CONFLICT (dedup_key) DO NOTHING",
                (url,),
            )
        cur.execute(
            "INSERT INTO apply_result_events ("
            "queue_name, url, worker_id, status, apply_status, apply_error, target_host, home_ip, "
            "agent, agent_model, est_cost_usd, apply_duration_ms, result_line, source"
            ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,0),%s,%s,%s)",
            (
                "apply_queue",
                url,
                worker_id,
                status,
                apply_status,
                apply_error,
                target_host,
                home_ip,
                agent,
                agent_model,
                est_cost_usd,
                apply_duration_ms,
                _result_line(status, apply_status=apply_status, apply_error=apply_error),
                "worker",
            ),
        )
        # Phase 2.1: the apply lane spends real agent money but never recorded it to
        # llm_usage, so every cost cap (_cost_cap_exceeded, fleet_config spend caps)
        # read $0 while real cost was incurred. Mirrors write_compute_result's insert;
        # 'apply_agent' matches the existing SQLite-brain stage convention (see
        # apply.supervisor._apply_cost_total). Only a real, positive cost is worth a row.
        if est_cost_usd is not None and float(est_cost_usd) > 0:
            # provider = the apply agent that spent it (claude/codex/deepseek) so
            # agent_budget.rolling_spend can sum per-agent spend for the predictive monitor.
            cur.execute(
                "INSERT INTO llm_usage (worker_id, task, provider, cost_usd) VALUES (%s,%s,%s,%s)",
                (worker_id, "apply_agent", agent, est_cost_usd),
            )
    conn.commit()
    return True


def suppress_applied_set_duplicates(conn, *, commit=True) -> int:
    """Retire queued apply rows whose dedup key is already in ``applied_set``.

    ``lease_apply`` refuses these rows to prevent double-submits. Keeping them in
    status='queued' makes the fleet look backlogged while every worker correctly
    idles, so convert them to a terminal skipped failure with an explicit reason.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE apply_queue q "
            "SET status='failed'::apply_queue_status, apply_status='skipped', "
            "apply_error='dedup:already_applied', lease_owner=NULL, lease_expires_at=NULL, "
            "updated_at=now() "
            "WHERE q.status='queued' AND q.dedup_key IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM applied_set a WHERE a.dedup_key = q.dedup_key)"
        )
        n = cur.rowcount
    if commit:
        conn.commit()
    return n


def push_apply_jobs(conn, rows, *, approved_batch=None, commit=True) -> int:
    """UPSERT apply_queue rows with the v3 columns (dedup_key, target_host, lane,
    approved_batch). Only refreshes ``queued`` rows. ``rows`` need url, company,
    title, application_url, score, and target_host (or apply_domain)."""
    prepared = []
    for r in rows:
        host = r.get("target_host") or r.get("apply_domain")
        dk = r.get("dedup_key") or _dedup.dedup_key(r.get("company"), r.get("title"))
        prepared.append((r, host, dk))

    applied_keys = set()
    with conn.cursor() as cur:
        keys = [dk for _, _, dk in prepared if dk]
        if keys:
            cur.execute("SELECT dedup_key FROM applied_set WHERE dedup_key = ANY(%s)", (keys,))
            applied_keys = {row["dedup_key"] for row in cur.fetchall()}

        n = 0
        for r, host, dk in prepared:
            if dk in applied_keys:
                continue
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


def park_challenge(conn, worker_id, url, *, commit=True) -> bool:
    """Freeze a leased apply row OUT of the reclaim pool while a human resolves an
    auth wall (R3 fail-safe). Keep status='leased' (so ``lease_apply`` -- which only
    picks status='queued' -- never re-picks it), but push ``lease_expires_at`` far
    out so ``apply.pgqueue.reclaim_stale_leases`` (status='leased' AND
    lease_expires_at < now()) never resets it and re-drives the SAME wall, blind,
    on another machine -- which is exactly how a residential IP gets burned. Mark
    the row challenge_pending for the owner queue. Lease-owner guarded; returns
    False if the lease is no longer held."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE apply_queue SET lease_expires_at = now() + interval '3650 days', "
            "apply_status='challenge_pending', apply_error=COALESCE(apply_error,'challenge_pending'), updated_at=now() "
            "WHERE url=%s AND lease_owner=%s AND status='leased'",
            (url, worker_id),
        )
        ok = cur.rowcount > 0
    if commit:
        conn.commit()
    return ok


def resolve_challenge(conn, url, *, requeue=True, commit=True) -> bool:
    """Owner-side release of a parked challenge (after the owner solves the wall on
    the trusted box, or gives up). ``requeue`` -> back to the pool for a retry from
    the owner machine; otherwise close it 'blocked'. Also resolves the open
    auth_challenge row(s). Returns True if a parked row was found."""
    with conn.cursor() as cur:
        if requeue:
            cur.execute(
                "UPDATE apply_queue SET status='queued', lease_owner=NULL, lease_expires_at=NULL, "
                "apply_status=NULL, apply_error=NULL, updated_at=now() "
                "WHERE url=%s AND apply_status='challenge_pending'",
                (url,),
            )
        else:
            cur.execute(
                "UPDATE apply_queue SET status='blocked', lease_owner=NULL, lease_expires_at=NULL, "
                "apply_status='challenge_skipped', updated_at=now() "
                "WHERE url=%s AND apply_status='challenge_pending'",
                (url,),
            )
        n = cur.rowcount
        cur.execute("UPDATE auth_challenge SET resolved_at=now(), outcome=%s WHERE url=%s AND resolved_at IS NULL",
                    ("solved" if requeue else "skipped", url))
    if commit:
        conn.commit()
    return n > 0


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
  SELECT url, task FROM compute_queue WHERE status='queued'
  ORDER BY updated_at, url, task LIMIT 1 FOR UPDATE SKIP LOCKED
)
UPDATE compute_queue c SET status='leased', lease_owner=%(worker)s,
  lease_expires_at = now() + make_interval(secs => %(ttl)s), attempts = c.attempts + 1, updated_at = now()
FROM next WHERE c.url = next.url AND c.task = next.task
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
                         model=None, provider=None, task=None, machine_owner=None,
                         tokens_in=None, tokens_out=None):
    effective_task = task
    if effective_task is None and isinstance(result, dict):
        effective_task = result.get("task")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE compute_queue SET status=%s, result=%s, est_cost_usd=COALESCE(%s,0), updated_at=now() "
            "WHERE url=%s AND lease_owner=%s AND (%s::text IS NULL OR task=%s)",
            (
                status,
                json.dumps(result) if result is not None else None,
                cost_usd,
                url,
                worker_id,
                effective_task,
                effective_task,
            ),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return False
        cur.execute(
            "INSERT INTO llm_usage (worker_id, machine_owner, task, model, provider, tokens_in, tokens_out, cost_usd) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (worker_id, machine_owner, task, model, provider, tokens_in, tokens_out, cost_usd),
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
                "ON CONFLICT (url, task) DO UPDATE SET payload=EXCLUDED.payload, est_cost_usd=EXCLUDED.est_cost_usd "
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
    AND COALESCE(g.breaker_state, 'ok') != 'demoted'
    AND COALESCE(NOT (g.breaker_state = 'paused' AND COALESCE(g.breaker_until, 'infinity'::timestamptz) >= now()), TRUE)
    AND COALESCE(g.count_24h, 0) < COALESCE(g.daily_cap, 2000000000)
    AND (COALESCE(g.last_applied_at, g.last_attempt_at) IS NULL
         OR COALESCE(g.last_applied_at, g.last_attempt_at) < now()
              - make_interval(secs => COALESCE(g.min_gap_seconds, 90) * (0.7 + random()*0.7)))
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
            # A3: stamp last_attempt_at on every board outcome so a never-succeeding board is spaced.
            extra = (", count_24h = count_24h + 1, last_applied_at = now(), last_attempt_at = now()"
                     if outcome == "success" else ", last_attempt_at = now()")
            sk = governor.board_scope(board)
            cur.execute("INSERT INTO rate_governor (scope_key) VALUES (%s) ON CONFLICT (scope_key) DO NOTHING", (sk,))
            cur.execute(f"UPDATE rate_governor SET {col} = {col} + 1{extra}, updated_at=now() WHERE scope_key=%s", (sk,))
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# LINKEDIN lease -- single-account mutex, owner-IP only (R1).
#
# A LinkedIn ban is the one catastrophe, so this lane is the strictest. The mutex
# is enforced at CLAIM time, not apply time: the lease locks the account:linkedin
# governor row (FOR UPDATE) so concurrent owner-IP machines serialize, and RESERVES
# the cap + min-gap right there (count_24h++ , last_applied_at=now()). Two coordinated
# machines therefore can NEVER hold two live sessions: the second blocks on the row
# lock, re-reads the just-stamped last_applied_at, fails the min-gap, and gets nothing.
# (Reserving at claim is deliberately conservative -- a crashed lease over-counts
# rather than risking a second concurrent session.)
#
# Lock-order note: cfg is locked FIRST (FOR UPDATE), then acct -- matching the order in
# _LEASE_APPLY (which locks fleet_config before home/glob governor rows). This prevents
# cross-lane deadlock between concurrent apply and linkedin leases. The canary decrement
# (canary CTE) is atomic because the 'account:linkedin' FOR UPDATE mutex already serializes
# ALL LinkedIn leases -- no separate cfg lock is needed for canary atomicity; cfg's FOR
# UPDATE is purely for lock-order consistency. At remaining=0 the linkedin_canary_remaining>0
# guard blocks (no paused flag, unlike the apply lane). min_gap=ttl (1200s) closes the
# halt-write race (§4.3): a row inserted by reserve at claim-time carries the same gap
# as the ttl, so the next claim cannot race in before the current one expires.
# ---------------------------------------------------------------------------
_LEASE_LINKEDIN = """
WITH cfg AS (SELECT linkedin_canary_enabled, linkedin_canary_remaining FROM fleet_config WHERE id=1 FOR UPDATE),
     acct AS (
       SELECT count_24h, daily_cap, last_applied_at, min_gap_seconds, breaker_state, breaker_until, halted_until
       FROM rate_governor WHERE scope_key = 'account:linkedin' FOR UPDATE
     ),
     next AS (
       SELECT q.url FROM linkedin_queue q LEFT JOIN acct a ON TRUE LEFT JOIN cfg ON TRUE
       WHERE q.status='queued' AND q.approved_batch IS NOT NULL
         AND (NOT COALESCE(cfg.linkedin_canary_enabled, FALSE) OR cfg.linkedin_canary_remaining > 0)
         AND (a.halted_until IS NULL OR a.halted_until < now())
         AND (a.count_24h IS NULL OR a.count_24h < a.daily_cap)
         AND COALESCE(a.breaker_state, 'ok') != 'demoted'
         AND COALESCE(NOT (a.breaker_state = 'paused' AND COALESCE(a.breaker_until, 'infinity'::timestamptz) >= now()), TRUE)
         AND (a.last_applied_at IS NULL OR a.last_applied_at < now() - make_interval(secs => COALESCE(a.min_gap_seconds, 1200)))
         AND NOT (
           LOWER(TRIM(COALESCE(q.company,''))) = ANY(%(blocked_names)s)
           OR q.url ILIKE ANY(%(blocked_pats)s)
           OR COALESCE(q.application_url,'') ILIKE ANY(%(blocked_pats)s)
         )
         AND NOT EXISTS (SELECT 1 FROM applied_set s WHERE s.dedup_key = q.dedup_key)
       ORDER BY q.score DESC, q.url LIMIT 1 FOR UPDATE OF q SKIP LOCKED
     ),
     reserve AS (
       UPDATE rate_governor SET count_24h = count_24h + 1, last_applied_at = now(), updated_at = now()
       WHERE scope_key = 'account:linkedin' AND EXISTS (SELECT 1 FROM next) RETURNING 1
     ),
     canary AS (
       UPDATE fleet_config SET linkedin_canary_remaining = linkedin_canary_remaining - 1
       WHERE id = 1 AND linkedin_canary_enabled AND EXISTS (SELECT 1 FROM next) RETURNING 1
     )
UPDATE linkedin_queue q SET status='leased', lease_owner=%(worker)s,
  lease_expires_at = now() + make_interval(secs => %(ttl)s), last_attempted_at=now(), attempts=q.attempts+1, updated_at=now()
FROM next WHERE q.url = next.url
RETURNING q.url, q.company, q.title, q.application_url, q.score;
"""


def lease_linkedin(conn, worker_id, *, public_ip, owner_ip, ttl_seconds=1200,
                   daily_cap=20, min_gap_seconds=1200):
    """LinkedIn lease: ONLY from the one owner IP, serialized by the account mutex.

    ``public_ip`` is the worker's REGISTERED egress IP and ``owner_ip`` is the
    broker-trusted owner IP -- both supplied server-side by the broker, never by the
    worker. They must match or the lease is refused outright."""
    if public_ip is None or owner_ip is None or public_ip != owner_ip:
        return None  # LinkedIn never from a different IP than the owner's
    blocked_names, blocked_pats = config.load_blocked_companies()
    with conn.cursor() as cur:
        # Ensure the account governor row EXISTS so the FOR UPDATE mutex has a row to
        # lock (otherwise concurrent leases wouldn't serialize). Preserves an existing cap.
        cur.execute(
            "INSERT INTO rate_governor (scope_key, daily_cap, min_gap_seconds, base_min_gap_seconds) "
            "VALUES ('account:linkedin', %s, %s, %s) ON CONFLICT (scope_key) DO NOTHING",
            (daily_cap, min_gap_seconds, min_gap_seconds),
        )
        cur.execute(_LEASE_LINKEDIN, {
            "worker": worker_id, "ttl": ttl_seconds,
            "blocked_names": list(blocked_names), "blocked_pats": blocked_pats,
        })
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


_REQUEUE_LINKEDIN_SQL = """
UPDATE linkedin_queue
SET status           = 'queued',
    apply_status     = NULL,
    apply_error      = %(apply_error)s,
    attempts         = GREATEST(attempts - 1, 0),
    lease_owner      = NULL,
    lease_expires_at = NULL,
    updated_at       = now()
WHERE url = %(url)s
  AND lease_owner = %(worker)s
RETURNING url;
"""

_REFUND_LINKEDIN_CANARY_SQL = """
UPDATE fleet_config
SET linkedin_canary_remaining = COALESCE(linkedin_canary_remaining, 0) + 1
WHERE id = 1
  AND linkedin_canary_enabled;
"""

_REFUND_LINKEDIN_ACCOUNT_SQL = """
WITH previous_attempt AS (
  SELECT MAX(last_attempted_at) AS last_at
  FROM linkedin_queue
  WHERE url <> %(url)s
    AND status <> 'queued'
    AND last_attempted_at IS NOT NULL
)
UPDATE rate_governor g
SET count_24h = GREATEST(COALESCE(g.count_24h, 0) - 1, 0),
    last_applied_at = previous_attempt.last_at,
    updated_at = now()
FROM previous_attempt
WHERE g.scope_key = 'account:linkedin';
"""


def requeue_linkedin(conn, worker_id, url, *, apply_error=None) -> bool:
    """Return a leased LinkedIn job to queued after a pre-browser usage/quota wall.

    `failed:usage_limit` is emitted only when the agent made zero browser tool calls, so
    re-queuing cannot double-submit. Lease-owner guarded like `requeue_apply`.
    Because the LinkedIn lease reserves the canary and account cap at claim time, this
    no-op path also refunds that reservation so a quota wall does not block real applies.
    """
    with conn.cursor() as cur:
        cur.execute(_REQUEUE_LINKEDIN_SQL, {"apply_error": apply_error, "worker": worker_id, "url": url})
        landed = cur.fetchone() is not None
        if landed:
            cur.execute(_REFUND_LINKEDIN_CANARY_SQL)
            cur.execute(_REFUND_LINKEDIN_ACCOUNT_SQL, {"url": url})
    conn.commit()
    return landed


def write_linkedin_result(conn, worker_id, url, *, status, apply_status=None, apply_error=None,
                          est_cost_usd=0, outcome=None, apply_channel=None, apply_external_host=None):
    """Close a LinkedIn lease (lease-owner guarded) in linkedin_queue -- NOT apply_queue --
    record the outcome on the account:linkedin governor, and UPSERT applied_set on a
    confirmed/possibly-submitted terminal. The cap (count_24h) + min-gap were already
    RESERVED at lease time, so this does NOT bump count_24h again. Returns False if the
    lease was lost."""
    if outcome is None:
        outcome = {"applied": "success", "blocked": "block", "captcha": "captcha"}.get(status)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE linkedin_queue SET status=%s, apply_status=%s, apply_error=%s, est_cost_usd=COALESCE(%s,0), "
            "apply_channel=COALESCE(%s, apply_channel), apply_external_host=COALESCE(%s, apply_external_host), "
            "applied_at = CASE WHEN %s = 'applied' THEN now() ELSE applied_at END, worker_id=%s, updated_at=now() "
            "WHERE url=%s AND lease_owner=%s",
            (status, apply_status, apply_error, est_cost_usd, apply_channel, apply_external_host,
             status, worker_id, url, worker_id),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return False
        if outcome is not None:
            sk = governor.LINKEDIN_ACCOUNT
            col = governor._OUTCOME_COL[outcome]
            extra = ", last_applied_at = now()" if status == "applied" else ""  # refresh gap on a confirmed apply
            cur.execute("INSERT INTO rate_governor (scope_key) VALUES (%s) ON CONFLICT (scope_key) DO NOTHING", (sk,))
            cur.execute(f"UPDATE rate_governor SET {col} = {col} + 1{extra}, updated_at=now() WHERE scope_key=%s", (sk,))
        if status in ("applied", "crash_unconfirmed"):
            cur.execute(
                "INSERT INTO applied_set (dedup_key, company, applied_url) "
                "SELECT dedup_key, company, application_url FROM linkedin_queue WHERE url=%s AND dedup_key IS NOT NULL "
                "ON CONFLICT (dedup_key) DO NOTHING",
                (url,),
            )
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# DISCOVERY staging -- lean workers push raw JobSpy postings here; home box ingests.
# ---------------------------------------------------------------------------
def _json_safe(value):
    if isinstance(value, numbers.Real) and not math.isfinite(float(value)):
        return None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def push_discovered(conn, *, task_id, source_label, worker_id, postings, commit=True) -> int:
    """Stage raw JobSpy postings (dicts) from a discovery worker into discovered_postings.
    The home box later ingests them into the brain (sync.pull_discovered)."""
    n = 0
    with conn.cursor() as cur:
        for p in postings:
            cur.execute(
                "INSERT INTO discovered_postings (task_id, source_label, posting, worker_id) "
                "VALUES (%s,%s,%s,%s)",
                (task_id, source_label, json.dumps(_json_safe(p), default=str, allow_nan=False), worker_id),
            )
            n += 1
    if commit:
        conn.commit()
    return n


def park_linkedin_challenge(conn, worker_id, url, *, halt_seconds, commit=True) -> bool:
    """Freeze the held linkedin_queue lease out of reclaim AND set the account halt, in ONE tx."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE linkedin_queue SET apply_status='challenge_pending', "
            "lease_expires_at = now() + interval '3650 days', updated_at=now() "
            "WHERE url=%s AND lease_owner=%s", (url, worker_id))
        if cur.rowcount == 0:
            conn.rollback()
            return False
        cur.execute("INSERT INTO rate_governor (scope_key) VALUES (%s) ON CONFLICT (scope_key) DO NOTHING",
                    (governor.LINKEDIN_ACCOUNT,))
        cur.execute("UPDATE rate_governor SET halted_until = now() + make_interval(secs => %s), updated_at=now() "
                    "WHERE scope_key=%s", (halt_seconds, governor.LINKEDIN_ACCOUNT))
    if commit:
        conn.commit()
    return True


def reclaim_linkedin(conn, *, grace_seconds=30, commit=True) -> int:
    """Stale linkedin_queue leases -> crash_unconfirmed, attempts=99, NEVER re-queued
    (a stale LinkedIn lease may have already submitted)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE linkedin_queue SET status='crash_unconfirmed', apply_error='crash_unconfirmed', "
            "attempts=99, lease_owner=NULL, lease_expires_at=NULL, updated_at=now() "
            "WHERE status='leased' AND lease_expires_at < now() - make_interval(secs => %s) "
            "RETURNING url", (grace_seconds,))
        n = len(cur.fetchall())
    if commit:
        conn.commit()
    return n


def _set_linkedin_halt(conn, value_sql, commit):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO rate_governor (scope_key) VALUES (%s) ON CONFLICT (scope_key) DO NOTHING",
                    (governor.LINKEDIN_ACCOUNT,))
        cur.execute(f"UPDATE rate_governor SET halted_until = {value_sql}, updated_at=now() WHERE scope_key=%s",
                    (governor.LINKEDIN_ACCOUNT,))
    if commit:
        conn.commit()


def clear_linkedin_halt(conn, *, commit=True):
    _set_linkedin_halt(conn, "NULL", commit)


def kill_linkedin(conn, *, commit=True):
    _set_linkedin_halt(conn, "now() + interval '36500 days'", commit)


# ---------------------------------------------------------------------------
# LINKEDIN OWNER HELPERS -- push/approve/resolve for the linkedin_queue.
# Mirrors push_apply_jobs / approve_jobs / resolve_challenge over linkedin_queue.
# ---------------------------------------------------------------------------

def push_linkedin_jobs(conn, rows, *, approved_batch=None, commit=True) -> int:
    """UPSERT linkedin_queue rows with dedup_key, lane='ats', and approved_batch.
    Only refreshes ``queued`` rows. ``rows`` need url, company, title,
    application_url, score. Uses the SAME _dedup.dedup_key as the offsite lane
    so cross-lane dedup (applied_set) works correctly."""
    n = 0
    with conn.cursor() as cur:
        for r in rows:
            dk = r.get("dedup_key") or _dedup.dedup_key(r.get("company"), r.get("title"))
            cur.execute(
                "INSERT INTO linkedin_queue (url, company, title, application_url, score, lane, dedup_key, approved_batch, linkedin_unresolved_kind, linkedin_next_action) "
                "VALUES (%(url)s,%(company)s,%(title)s,%(application_url)s,%(score)s,'ats',%(dk)s,%(batch)s,%(linkedin_unresolved_kind)s,%(linkedin_next_action)s) "
                "ON CONFLICT (url) DO UPDATE SET company=EXCLUDED.company, title=EXCLUDED.title, "
                "application_url=EXCLUDED.application_url, score=EXCLUDED.score, "
                "dedup_key=EXCLUDED.dedup_key, "
                "linkedin_unresolved_kind=EXCLUDED.linkedin_unresolved_kind, "
                "linkedin_next_action=EXCLUDED.linkedin_next_action, "
                "approved_batch=COALESCE(EXCLUDED.approved_batch, linkedin_queue.approved_batch), updated_at=now() "
                "WHERE linkedin_queue.status='queued'",
                {**r, "dk": dk, "batch": approved_batch, "linkedin_unresolved_kind": r.get("linkedin_unresolved_kind"), "linkedin_next_action": r.get("linkedin_next_action")},
            )
            n += cur.rowcount
    if commit:
        conn.commit()
    return n


def approve_linkedin_jobs(conn, urls, batch, *, commit=True) -> int:
    """Stamp the owner approval token on queued linkedin_queue rows."""
    with conn.cursor() as cur:
        cur.execute("UPDATE linkedin_queue SET approved_batch=%s, updated_at=now() "
                    "WHERE url = ANY(%s) AND status='queued'", (batch, list(urls)))
        n = cur.rowcount
    if commit:
        conn.commit()
    return n


def resolve_linkedin_challenge(conn, url, *, requeue=True, commit=True) -> bool:
    """Owner-side release of a parked LinkedIn challenge. ``requeue`` -> back to the pool
    for a retry from the owner machine; otherwise close it 'blocked'. Also resolves the open
    auth_challenge row(s). Returns True if a parked row was found."""
    with conn.cursor() as cur:
        if requeue:
            cur.execute(
                "UPDATE linkedin_queue SET status='queued', lease_owner=NULL, lease_expires_at=NULL, "
                "apply_status=NULL, apply_error=NULL, updated_at=now() "
                "WHERE url=%s AND apply_status='challenge_pending'",
                (url,),
            )
        else:
            cur.execute(
                "UPDATE linkedin_queue SET status='blocked', lease_owner=NULL, lease_expires_at=NULL, "
                "apply_status='challenge_skipped', updated_at=now() "
                "WHERE url=%s AND apply_status='challenge_pending'",
                (url,),
            )
        n = cur.rowcount
        cur.execute("UPDATE auth_challenge SET resolved_at=now(), outcome=%s WHERE url=%s AND resolved_at IS NULL",
                    ("solved" if requeue else "skipped", url))
    if commit:
        conn.commit()
    return n > 0


# ---------------------------------------------------------------------------
# Challenge summary -- SHARED kind x host x count view over the open
# auth_challenge backlog UNION the lane's parked (apply_status='challenge_pending')
# rows. This is the single source both the console's build_challenges() (detail
# view, one row per job) and the CLI `challenges --grouped` flag on both home
# mains (a plain kind x host -> count table) key off of, so the host/kind
# derivation can never diverge between the two surfaces.
# ---------------------------------------------------------------------------
def host_of(url: str | None) -> str:
    """netloc of url, www. stripped. Best-effort -- never raises on a malformed url."""
    if not url:
        return ""
    try:
        host = _urlparse.urlsplit(url).hostname or ""
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _challenge_rows(conn, lane: str | None):
    """READ-ONLY: fetch the open auth_challenge rows + the lane's parked rows
    (apply_queue for lane='apply', linkedin_queue for lane='linkedin', BOTH for
    lane=None). Returns (open_challenges, parked_by_url) where parked_by_url maps
    url -> (lane_name, row). conn.rollback() marks it read-only."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT url, kind, machine_owner, screenshot_url, raised_at "
            "FROM auth_challenge WHERE resolved_at IS NULL")
        open_challenges = cur.fetchall()
        parked_apply = []
        parked_linkedin = []
        if lane in (None, "apply"):
            cur.execute(
                "SELECT url, company, title, score, updated_at "
                "FROM apply_queue WHERE apply_status='challenge_pending'")
            parked_apply = cur.fetchall()
        if lane in (None, "linkedin"):
            cur.execute(
                "SELECT url, company, title, score, updated_at "
                "FROM linkedin_queue WHERE apply_status='challenge_pending'")
            parked_linkedin = cur.fetchall()
    conn.rollback()  # read-only

    parked_by_url: dict[str, tuple[str, dict]] = {}
    for r in parked_apply:
        parked_by_url[r["url"]] = ("apply", r)
    for r in parked_linkedin:
        parked_by_url[r["url"]] = ("linkedin", r)

    if lane is not None:
        # A lane-scoped view only ever shows open auth_challenge rows whose url is
        # ALSO parked in that same lane's queue -- a bare open challenge with no
        # parked row in either queue isn't attributable to one lane, so it's
        # excluded once we've narrowed to a single lane.
        open_challenges = [c for c in open_challenges
                            if parked_by_url.get(c["url"], (None, None))[0] == lane]

    return open_challenges, parked_by_url


def challenge_summary(conn, lane: str | None) -> list[dict]:
    """kind x host -> count rows over open auth_challenge UNION the lane's parked
    rows (lane=None -> both queues). Reuses the exact host/kind derivation
    build_challenges() (console_app.py) uses for its detail view, so the two
    surfaces can never diverge.

    ``lane`` in {None, 'apply', 'linkedin'}. Each returned dict is
    {'kind': str, 'host': str, 'count': int}."""
    open_challenges, parked_by_url = _challenge_rows(conn, lane)

    challenge_by_url: dict[str, dict] = {r["url"]: r for r in open_challenges}
    urls = set(challenge_by_url) | set(parked_by_url)

    counts: dict[tuple[str, str], int] = {}
    for url in urls:
        ch = challenge_by_url.get(url)
        kind = ch["kind"] if ch else "(no challenge row)"
        host = host_of(url)
        counts[(kind, host)] = counts.get((kind, host), 0) + 1

    return [{"kind": kind, "host": host, "count": n} for (kind, host), n in counts.items()]


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
