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
import os
import time
import urllib.parse as _urlparse
from collections.abc import Mapping

from psycopg.errors import DeadlockDetected
from psycopg.types.json import Jsonb

from applypilot import config
from applypilot.fleet import config as fleet_config
from applypilot.fleet import dedup as _dedup
from applypilot.fleet import governor


def version_mismatch(conn, worker_id: str, sw_version: str | None) -> str | None:
    """Return the required version when a reporting worker must not lease work."""
    if sw_version is None:
        return None
    expected = fleet_config.version_for_worker(conn, worker_id)
    if expected and sw_version != expected:
        return expected
    return None

def _nonnegative_env_seconds(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


LIVENESS_FRESH_SECONDS = _nonnegative_env_seconds("APPLYPILOT_LIVENESS_FRESH_SECONDS", 900)
LIVENESS_UNCERTAIN_RETRY_SECONDS = _nonnegative_env_seconds(
    "APPLYPILOT_LIVENESS_UNCERTAIN_RETRY_SECONDS", 21_600
)
LIVENESS_TRANSIENT_RETRY_SECONDS = _nonnegative_env_seconds(
    "APPLYPILOT_LIVENESS_TRANSIENT_RETRY_SECONDS", 3_600
)
LIVENESS_STRUCTURAL_RETRY_SECONDS = _nonnegative_env_seconds(
    "APPLYPILOT_LIVENESS_STRUCTURAL_RETRY_SECONDS", 86_400
)
LIVENESS_HOST_COOLDOWN_SECONDS = _nonnegative_env_seconds(
    "APPLYPILOT_LIVENESS_HOST_COOLDOWN_SECONDS", 1_800
)


def liveness_retry_policy(reason: str | None, consecutive_uncertain: int = 0) -> tuple[str, int]:
    """Return the scheduler class and retry delay for an uncertain reason."""
    value = reason or ""
    if (
        value.endswith(("_invalid_payload", "_id_mismatch"))
        or value in {"redirect_login", "redirect_home", "empty_body", "thin_body", "workday_unparsed"}
        or consecutive_uncertain >= 6
    ):
        return "structural", LIVENESS_STRUCTURAL_RETRY_SECONDS
    if consecutive_uncertain >= 3:
        return "uncertain", LIVENESS_UNCERTAIN_RETRY_SECONDS
    if value.startswith(("server_", "neterr:", "error:")) or value == "blocked_429":
        return "transient", LIVENESS_TRANSIENT_RETRY_SECONDS
    return "uncertain", LIVENESS_UNCERTAIN_RETRY_SECONDS


CANONICAL_QUEUE_FIELDS = frozenset({
    "decision_id", "policy_version", "decision_action", "qualification_verdict",
    "qualification_score", "qualification_floor", "preference_score", "outcome_score",
    "final_score", "decision_confidence", "decision_created_at", "decision_expires_at",
    "input_hash",
})


def _sync_worker_lease_authority(conn, *, linkedin_owner_ip=None, home_ip=None) -> bool:
    """Refresh controller-owned lease inputs when called by a privileged connection."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege(current_user,'public.fleet_worker_blocklist','INSERT,DELETE') "
            "AS controller"
        )
        if not cur.fetchone()["controller"]:
            return False
        blocked_names, blocked_patterns = config.load_blocked_companies()
        cur.execute("DELETE FROM public.fleet_worker_blocklist")
        cur.executemany(
            "INSERT INTO public.fleet_worker_blocklist(kind,value) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING",
            [*(('company', value) for value in blocked_names),
             *(('pattern', value) for value in blocked_patterns)],
        )
        if linkedin_owner_ip is not None:
            cur.execute(
                "UPDATE public.fleet_config SET linkedin_owner_ip=%s WHERE id=1",
                (linkedin_owner_ip,),
            )
        if home_ip is not None:
            cur.execute(
                "INSERT INTO public.rate_governor(scope_key) VALUES ('global'),(%s) "
                "ON CONFLICT(scope_key) DO NOTHING",
                (governor.home_ip_scope(home_ip),),
            )
            cur.execute(
                "INSERT INTO public.rate_governor(scope_key) "
                "SELECT DISTINCT 'host:'||COALESCE(target_host,apply_domain) "
                "FROM public.apply_queue WHERE status='queued' "
                "AND COALESCE(target_host,apply_domain) IS NOT NULL "
                "ON CONFLICT(scope_key) DO NOTHING"
            )
    return True


def _has_controller_table_authority(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_catalog.has_table_privilege(current_user,%s,'UPDATE') AS allowed",
            (f"public.{table}",),
        )
        return bool(cur.fetchone()["allowed"])


def _validate_canonical_queue_row(row: Mapping[str, object]) -> None:
    missing = sorted(CANONICAL_QUEUE_FIELDS - row.keys())
    nulls = sorted(key for key in CANONICAL_QUEUE_FIELDS if key in row and row[key] is None)
    if missing or nulls:
        raise ValueError(
            f"canonical decision provenance required: missing={missing}, null={nulls}"
        )
    if row["decision_action"] != "apply" or row["qualification_verdict"] != "qualified":
        raise ValueError("canonical apply/qualified decision provenance required")
    for key in (
        "qualification_score", "qualification_floor", "preference_score", "outcome_score",
        "final_score", "decision_confidence",
    ):
        value = row[key]
        if not isinstance(value, numbers.Real) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f"canonical provenance {key} must be finite")
    if row["qualification_score"] < row["qualification_floor"]:
        raise ValueError("canonical qualification score is below its policy floor")
    if not 0 <= row["decision_confidence"] <= 1:
        raise ValueError("canonical decision confidence must be between 0 and 1")
    if row.get("score") != row["final_score"]:
        raise ValueError("queue score must equal canonical final_score")
    for key in ("decision_id", "policy_version", "input_hash"):
        if not isinstance(row[key], str) or not row[key].strip():
            raise ValueError(f"canonical provenance {key} must be nonblank")

# ---------------------------------------------------------------------------
# APPLY lease -- governed, approval-gated, dedup-guarded (R6, R9, R11).
#
# Atomicity notes (catastrophe gate):
#   - cfg … FOR UPDATE row-locks the single fleet_config row, serializing ALL
#     apply leases on it — exactly as _LEASE_LINKEDIN locks 'account:linkedin'.
#     No two transactions can pass the canary guard simultaneously.
#   - After the queue row is leased, fleet_worker_authorize_lease rechecks the
#     locked config/policy gates and performs any canary decrement. A failed
#     authorization aborts this transaction, rolling the queue lease back.
#   - ats_apply_mode is lane-local. Exhausting an ATS canary stops only the ATS
#     lane; it never writes the shared paused flag that also gates LinkedIn.
# ---------------------------------------------------------------------------
_LEASE_APPLY = """
WITH cfg AS (SELECT ats_apply_mode, canary_enabled, canary_remaining, paused, ats_paused,
                    spend_cap_usd, ats_policy_version, approval_threshold
             FROM fleet_config WHERE id=1 FOR UPDATE),
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
         AND q.decision_id IS NOT NULL
         AND q.policy_version = cfg.ats_policy_version
         AND q.decision_action = 'apply'
         AND q.qualification_verdict = 'qualified'
         AND q.qualification_score >= q.qualification_floor
         AND q.decision_expires_at > now()
          AND q.score = q.final_score
          AND (cfg.approval_threshold IS NULL OR q.final_score >= cfg.approval_threshold)
          AND COALESCE(q.apply_error, '') NOT ILIKE 'requeued_by_%%'
          AND NOT EXISTS (
            SELECT 1
            FROM apply_result_events prior
            WHERE prior.queue_name = 'apply_queue'
              AND prior.url = q.url
              AND (
                COALESCE(prior.application_tool_calls, 0) > 0
                OR COALESCE(prior.apply_error, '') ILIKE 'requeued_by_%%'
              )
          )
          AND EXISTS (
           SELECT 1 FROM fleet_decision_policies p
           WHERE p.policy_version = q.policy_version AND p.lane = 'ats'
              AND p.status IN ('canary', 'active')
         )
         AND NOT COALESCE(cfg.paused, FALSE)
         -- H1: the Fleet Doctor's lane-pause writes ats_paused (NEVER the shared paused flag),
         -- honored here on the ATS lane only. _LEASE_LINKEDIN never reads ats_paused, so a
         -- Doctor pause can never halt the LinkedIn catastrophe lane.
         AND NOT COALESCE(cfg.ats_paused, FALSE)
         -- Lane policy is explicit: stopped blocks, canary requires positive
         -- ATS canary capacity, steady allows leasing without touching canary.
         AND (
              COALESCE(cfg.ats_apply_mode, 'stopped') = 'steady'
              OR (
                   COALESCE(cfg.ats_apply_mode, 'stopped') = 'canary'
                   AND COALESCE(cfg.canary_enabled, FALSE)
                   AND COALESCE(cfg.canary_remaining, 0) > 0
              )
         )
         AND (COALESCE(cfg.spend_cap_usd, 0) <= 0
              OR (SELECT COALESCE(SUM(cumulative_cost_usd), 0) FROM apply_queue) < cfg.spend_cap_usd)
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
         AND NOT EXISTS (
           SELECT 1 FROM apply_attempts aa
           WHERE aa.dedup_key = q.dedup_key
             AND aa.state IN ('submit_started', 'submitted_unverified')
         )
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
         AND (
              NOT COALESCE(q.liveness_required, FALSE)
              OR (
                   q.liveness_status = 'live'
                   AND q.liveness_checked_at >= now() - make_interval(secs => %(liveness_fresh)s)
              )
         )
         AND (
              NOT COALESCE(q.eligibility_required, FALSE)
              OR q.eligibility_status = 'eligible'
         )
         AND (
              NOT COALESCE(q.routing_required, FALSE)
              OR q.execution_route = 'deterministic'
         )
       ORDER BY q.score DESC, q.url
       LIMIT 1
       FOR UPDATE OF q SKIP LOCKED
     )
UPDATE apply_queue q
SET status='leased', lease_owner=%(worker)s, lease_expires_at = now() + make_interval(secs => %(ttl)s),
    last_attempted_at = now(), attempts = q.attempts + 1, updated_at = now(), worker_home_ip = %(home_ip)s
FROM next_job
WHERE q.url = next_job.url
RETURNING q.url, q.company, q.title, q.application_url,
          COALESCE(q.target_host, q.apply_domain) AS target_host, q.score, q.dedup_key, q.attempts,
          q.session_required, q.tenant_profile_id, q.execution_route, q.host_policy;
"""


def lease_apply(conn, worker_id, *, home_ip, ttl_seconds=1200, sw_version=None,
                liveness_fresh_seconds=LIVENESS_FRESH_SECONDS):
    if _has_controller_table_authority(conn, "fleet_config"):
        _sync_worker_lease_authority(conn, home_ip=home_ip)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM public.fleet_worker_lease_ats(%s,%s,%s,%s,%s)",
            (worker_id, home_ip, ttl_seconds, sw_version, liveness_fresh_seconds),
        )
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


_CLAIM_LIVENESS_SQL = """
WITH candidate AS (
    SELECT q.url
    FROM apply_queue q
    JOIN fleet_config cfg ON cfg.id = 1
    WHERE q.status = 'queued'
      AND q.lane = 'ats'
      AND q.approved_batch IS NOT NULL
      AND q.score >= COALESCE(cfg.approval_threshold, 7)
      AND COALESCE(q.liveness_required, FALSE)
       AND (
            q.liveness_checked_at IS NULL
           OR (
                q.liveness_status = 'uncertain'
                AND q.liveness_checked_at < now() - make_interval(secs => CASE
                    WHEN COALESCE(q.liveness_reason,'') LIKE '%%_invalid_payload'
                      OR COALESCE(q.liveness_reason,'') LIKE '%%_id_mismatch'
                      OR COALESCE(q.liveness_reason,'') IN (
                          'redirect_login', 'redirect_home', 'empty_body',
                          'thin_body', 'workday_unparsed'
                      )
                      OR COALESCE(q.liveness_consecutive_uncertain, 0) >= 6
                    THEN %(structural_retry)s
                    WHEN COALESCE(q.liveness_consecutive_uncertain, 0) >= 3
                    THEN %(uncertain_retry)s
                    WHEN COALESCE(q.liveness_reason,'') LIKE 'server_%%'
                      OR COALESCE(q.liveness_reason,'') LIKE 'neterr:%%'
                      OR COALESCE(q.liveness_reason,'') LIKE 'error:%%'
                      OR COALESCE(q.liveness_reason,'') = 'blocked_429'
                    THEN %(transient_retry)s
                    ELSE %(uncertain_retry)s
                END)
           )
           OR (
                q.liveness_status IS DISTINCT FROM 'uncertain'
                AND q.liveness_checked_at < now() - make_interval(secs => %(fresh)s)
           )
      )
      AND (
           q.liveness_check_owner IS NULL
           OR q.liveness_check_expires_at < now()
      )
      AND NOT EXISTS (
          SELECT 1
          FROM apply_queue host_peer
          WHERE host_peer.url <> q.url
            AND COALESCE(q.target_host, q.apply_domain, '') <> ''
            AND COALESCE(host_peer.target_host, host_peer.apply_domain, '') =
                COALESCE(q.target_host, q.apply_domain, '')
            AND (
                (
                    host_peer.liveness_check_owner IS NOT NULL
                    AND host_peer.liveness_check_expires_at >= now()
                )
                OR (
                    host_peer.liveness_status = 'uncertain'
                    AND host_peer.liveness_checked_at >=
                        now() - make_interval(secs => %(host_cooldown)s)
                    AND (
                        COALESCE(host_peer.liveness_reason,'') LIKE 'server_%%'
                        OR COALESCE(host_peer.liveness_reason,'') LIKE 'neterr:%%'
                        OR COALESCE(host_peer.liveness_reason,'') LIKE 'error:%%'
                        OR COALESCE(host_peer.liveness_reason,'') = 'blocked_429'
                        OR COALESCE(host_peer.liveness_reason,'') LIKE 'access_gate:%%'
                    )
                )
            )
      )
    ORDER BY CASE
               WHEN q.liveness_status = 'live' THEN 0
               WHEN q.liveness_status IS NULL THEN 1
               ELSE 2
             END,
             q.score DESC, q.url
    LIMIT 1
    FOR UPDATE OF q SKIP LOCKED
)
UPDATE apply_queue q
SET liveness_check_owner = %(worker)s,
    liveness_check_expires_at = now() + make_interval(secs => %(ttl)s),
    updated_at = now()
FROM candidate
WHERE q.url = candidate.url
RETURNING q.url, q.application_url, q.company, q.title,
          COALESCE(q.target_host, q.apply_domain) AS target_host;
"""


def claim_liveness_check(conn, worker_id, *, ttl_seconds=120,
                         fresh_seconds=LIVENESS_FRESH_SECONDS,
                         uncertain_retry_seconds=LIVENESS_UNCERTAIN_RETRY_SECONDS,
                         transient_retry_seconds=LIVENESS_TRANSIENT_RETRY_SECONDS,
                         structural_retry_seconds=LIVENESS_STRUCTURAL_RETRY_SECONDS,
                         host_cooldown_seconds=LIVENESS_HOST_COOLDOWN_SECONDS):
    """Claim one stale required-liveness row without leasing an application."""
    if not _has_controller_table_authority(conn, "apply_queue"):
        with conn.cursor() as cur:
            cur.execute("SELECT public.fleet_worker_claim_liveness() AS job")
            row = cur.fetchone()["job"]
        conn.commit()
        return dict(row) if row else None
    with conn.cursor() as cur:
        cur.execute(_CLAIM_LIVENESS_SQL, {
            "worker": worker_id,
            "ttl": ttl_seconds,
            "fresh": fresh_seconds,
            "uncertain_retry": uncertain_retry_seconds,
            "transient_retry": transient_retry_seconds,
            "structural_retry": structural_retry_seconds,
            "host_cooldown": host_cooldown_seconds,
        })
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


def write_liveness_result(conn, worker_id, url, *, status, reason) -> bool:
    """Commit an owner-guarded preflight verdict; confirmed dead rows terminate."""
    if not _has_controller_table_authority(conn, "apply_queue"):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT public.fleet_worker_write_liveness(%s,%s,%s) AS ok",
                (url, status, reason),
            )
            landed = cur.fetchone()["ok"]
        conn.commit()
        return landed
    normalized = status if status in {"live", "dead", "uncertain"} else "uncertain"
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE apply_queue SET liveness_status=%s, liveness_reason=%s, "
            "liveness_checked_at=now(), liveness_check_owner=NULL, "
            "liveness_check_expires_at=NULL, "
            "liveness_check_count=COALESCE(liveness_check_count,0)+1, "
            "liveness_consecutive_uncertain=CASE WHEN %s='uncertain' "
            "THEN COALESCE(liveness_consecutive_uncertain,0)+1 ELSE 0 END, "
            "status=CASE WHEN %s='dead' THEN 'failed'::apply_queue_status ELSE status END, "
            "apply_status=CASE WHEN %s='dead' THEN 'expired' ELSE apply_status END, "
            "apply_error=CASE WHEN %s='dead' THEN %s ELSE apply_error END, updated_at=now() "
            "WHERE url=%s AND liveness_check_owner=%s RETURNING url",
            (
                normalized,
                reason[:200] if reason else None,
                normalized,
                normalized,
                normalized,
                normalized,
                f"liveness:{reason or 'dead'}"[:200],
                url,
                worker_id,
            ),
        )
        landed = cur.fetchone() is not None
    conn.commit()
    return landed


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
        cur.execute(
            "SELECT public.fleet_worker_requeue('ats',%s,%s,%s) AS landed",
            (url, worker_id, apply_error),
        )
        landed = cur.fetchone()["landed"]
    conn.commit()
    return landed


def park_infrastructure_failure(conn, worker_id, url, *, apply_error) -> dict | None:
    """Park an untouched row after its one local component restart also failed."""
    landed = _terminalize(
        conn, "ats", worker_id, url, status="failed",
        apply_status="infrastructure_pending",
        apply_error=(apply_error[:200] if apply_error else "browser_preflight"), evidence={},
        commit=False,
    )
    conn.commit()
    return {"status": "failed", "apply_status": "infrastructure_pending"} if landed else None


def _result_line(status, apply_status=None, apply_error=None) -> str:
    if status == "applied":
        return "RESULT:APPLIED"
    token = apply_error or apply_status or status or "unknown"
    return f"RESULT:{token}"


def _terminalize(
    conn, lane, worker_id, url, *, status, apply_status, apply_error, evidence, commit=True
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT public.fleet_worker_terminalize(%s,%s,%s,%s,%s,%s,%s) AS landed",
            (lane, url, worker_id, status, apply_status, apply_error, Jsonb(evidence)),
        )
        landed = cur.fetchone()["landed"]
    if commit:
        conn.commit()
    return landed


def resolve_superseded_challenges(cur, url, *, terminal_status, queue_name) -> int:
    """Close open auth records after the queue row has conclusively moved on.

    The caller must first complete its guarded queue transition in the same
    transaction. Keeping this cursor-local prevents a stale lease owner from
    resolving a challenge when its queue update did not land.
    """
    other_queue = {
        "apply_queue": "linkedin_queue",
        "linkedin_queue": "apply_queue",
    }.get(queue_name)
    if other_queue is None:
        raise ValueError(f"unsupported queue_name: {queue_name}")
    cur.execute(
        "UPDATE auth_challenge SET resolved_at=now(), outcome=%s "
        "WHERE url=%s AND resolved_at IS NULL "
        f"AND NOT EXISTS (SELECT 1 FROM {other_queue} "
        "                WHERE url=%s AND apply_status='challenge_pending')",
        (f"superseded:{terminal_status}", url, url),
    )
    return cur.rowcount


def write_apply_result(conn, worker_id, url, *, status, target_host, home_ip,
                        apply_status=None, apply_error=None, est_cost_usd=0, outcome=None,
                        agent=None, agent_model=None, apply_duration_ms=None, machine_owner=None,
                        route=None, failure_class=None, tool_calls_total=None,
                        application_tool_calls=None, last_tool=None, host_policy=None,
                        job_log_path=None, transcript_digest=None,
                        final_result_source=None, result_metadata=None):
    """Record a lease-bound worker assertion and park claimed submissions.

    This never creates canonical applied state or ``applied_set``. Independent
    controller verification performs that promotion in a separate boundary.

    ``outcome`` in {'success','captcha','block'}; derived from ``status`` if None.
    A generic page/form failure derives to None and records NO challenge outcome,
    so it cannot pollute the captcha_24h leading indicator that drives the breaker.
    Returns False if the lease was lost (already reclaimed/closed)."""
    return _terminalize(
        conn,
        "ats",
        worker_id,
        url,
        status=status,
        apply_status=apply_status,
        apply_error=apply_error,
        evidence={
            "asserted_target_host": target_host,
            "asserted_home_ip": home_ip,
            "est_cost_usd": est_cost_usd,
            "agent": agent,
            "agent_model": agent_model,
            "apply_duration_ms": apply_duration_ms,
            "machine_owner": machine_owner,
            "route": route,
            "failure_class": failure_class,
            "tool_calls_total": tool_calls_total,
            "application_tool_calls": application_tool_calls,
            "last_tool": last_tool,
            "host_policy": host_policy,
            "job_log_path": job_log_path,
            "transcript_digest": transcript_digest,
            "final_result_source": final_result_source,
            "result_metadata": result_metadata or {},
            "asserted_outcome": outcome,
        },
    )


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


def retire_queued_dead_jobs(
    conn,
    *,
    dead_urls: list[str] | None = None,
    dead_application_urls: list[str] | None = None,
    table: str = "apply_queue",
    commit: bool = True,
) -> int:
    """Retire queued apply rows whose URL/apply URL now points at local-dead postings.

    This keeps queue visibility accurate when local liveness marks a posting dead after
    it was already staged: it prevents stale terminal jobs from staying in the
    `queued` bucket and blocking readiness checks.
    """
    if table not in {"apply_queue", "linkedin_queue"}:
        raise ValueError(f"unsupported queue table: {table}")
    urls = [u for u in (dead_urls or []) if u]
    app_urls = [u for u in (dead_application_urls or []) if u]
    if not urls and not app_urls:
        return 0

    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {table} q "
            "SET status='failed'::apply_queue_status, apply_status='failed', apply_error='expired', "
            "lease_owner=NULL, lease_expires_at=NULL, updated_at=now() "
            "WHERE q.status='queued' "
            "AND (q.url = ANY(%(urls)s) OR COALESCE(q.application_url,'') = ANY(%(app_urls)s))",
            {"urls": urls, "app_urls": app_urls},
        )
        n = cur.rowcount
    if commit:
        conn.commit()
    return n


def push_apply_jobs(conn, rows, *, approved_batch=None, require_liveness=False,
                    require_eligibility=False, commit=True, diagnostics=False) -> int | dict:
    """UPSERT apply_queue rows with the v3 columns (dedup_key, target_host, lane,
    approved_batch). Only refreshes ``queued`` rows. ``rows`` need url, company,
    title, application_url, score, and target_host (or apply_domain)."""
    prepared = []
    for r in rows:
        _validate_canonical_queue_row(r)
        host = r.get("target_host") or r.get("apply_domain")
        dk = r.get("dedup_key") or _dedup.dedup_key(r.get("company"), r.get("title"))
        prepared.append((r, host, dk))

    applied_keys = set()
    weak_applied_keys = set()
    with conn.cursor() as cur:
        keys = [dk for _, _, dk in prepared if dk]
        if keys:
            cur.execute(
                "SELECT dedup_key, company, normalized_role, applied_url "
                "FROM applied_set WHERE dedup_key = ANY(%s)",
                (keys,),
            )
            applied_rows = cur.fetchall()
            applied_keys = {row["dedup_key"] for row in applied_rows}
            weak_applied_keys = {
                row["dedup_key"] for row in applied_rows
                if not any(row[field] for field in ("company", "normalized_role", "applied_url"))
            }

        n = 0
        dedup_skipped = 0
        for r, host, dk in prepared:
            if dk in applied_keys:
                dedup_skipped += 1
                continue
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, apply_domain, target_host, lane, dedup_key, approved_batch, liveness_required, liveness_status, liveness_reason, liveness_checked_at, eligibility_required, eligibility_status, eligibility_reason, eligibility_checked_at, session_required, tenant_profile_id, routing_required, execution_route, host_policy, status, apply_status, apply_error, decision_id, policy_version, decision_action, qualification_verdict, qualification_score, qualification_floor, preference_score, outcome_score, final_score, decision_confidence, decision_created_at, decision_expires_at, input_hash) "
                "VALUES (%(url)s,%(company)s,%(title)s,%(application_url)s,%(score)s,%(host)s,%(host)s,'ats',%(dk)s,%(batch)s,%(require_liveness)s,%(liveness_status)s,%(liveness_reason)s,%(liveness_checked_at)s,%(require_eligibility)s,%(eligibility_status)s,%(eligibility_reason)s,CASE WHEN %(require_eligibility)s THEN now() ELSE NULL END,%(session_required)s,%(tenant_profile_id)s,%(routing_required)s,%(execution_route)s,%(host_policy)s,CASE WHEN (%(require_eligibility)s AND %(eligibility_status)s='ineligible') OR %(execution_route)s='exception' THEN 'failed'::apply_queue_status ELSE 'queued'::apply_queue_status END,CASE WHEN %(require_eligibility)s AND %(eligibility_status)s='ineligible' THEN 'failed' WHEN %(execution_route)s='exception' THEN 'exception_pending' ELSE NULL END,CASE WHEN %(require_eligibility)s AND %(eligibility_status)s='ineligible' THEN %(eligibility_reason)s WHEN %(execution_route)s='exception' THEN %(host_policy)s ELSE NULL END,%(decision_id)s,%(policy_version)s,%(decision_action)s,%(qualification_verdict)s,%(qualification_score)s,%(qualification_floor)s,%(preference_score)s,%(outcome_score)s,%(final_score)s,%(decision_confidence)s,%(decision_created_at)s,%(decision_expires_at)s,%(input_hash)s) "
                "ON CONFLICT (url) DO UPDATE SET company=EXCLUDED.company, title=EXCLUDED.title, "
                "application_url=EXCLUDED.application_url, score=EXCLUDED.score, target_host=EXCLUDED.target_host, "
                "dedup_key=EXCLUDED.dedup_key, "
                "liveness_required=EXCLUDED.liveness_required, "
                "eligibility_required=EXCLUDED.eligibility_required, eligibility_status=EXCLUDED.eligibility_status, "
                "eligibility_reason=EXCLUDED.eligibility_reason, eligibility_checked_at=EXCLUDED.eligibility_checked_at, "
                "session_required=EXCLUDED.session_required, tenant_profile_id=EXCLUDED.tenant_profile_id, "
                "routing_required=EXCLUDED.routing_required, execution_route=EXCLUDED.execution_route, "
                "host_policy=EXCLUDED.host_policy, "
                "decision_id=EXCLUDED.decision_id, policy_version=EXCLUDED.policy_version, "
                "decision_action=EXCLUDED.decision_action, qualification_verdict=EXCLUDED.qualification_verdict, "
                "qualification_score=EXCLUDED.qualification_score, qualification_floor=EXCLUDED.qualification_floor, "
                "preference_score=EXCLUDED.preference_score, outcome_score=EXCLUDED.outcome_score, final_score=EXCLUDED.final_score, "
                "decision_confidence=EXCLUDED.decision_confidence, decision_created_at=EXCLUDED.decision_created_at, "
                "decision_expires_at=EXCLUDED.decision_expires_at, input_hash=EXCLUDED.input_hash, "
                "status=EXCLUDED.status, apply_status=EXCLUDED.apply_status, apply_error=EXCLUDED.apply_error, "
                "liveness_status=CASE "
                "WHEN apply_queue.application_url IS DISTINCT FROM EXCLUDED.application_url THEN EXCLUDED.liveness_status "
                "WHEN EXCLUDED.liveness_checked_at IS NOT NULL AND (apply_queue.liveness_checked_at IS NULL OR EXCLUDED.liveness_checked_at > apply_queue.liveness_checked_at) THEN EXCLUDED.liveness_status "
                "ELSE apply_queue.liveness_status END, "
                "liveness_reason=CASE "
                "WHEN apply_queue.application_url IS DISTINCT FROM EXCLUDED.application_url THEN EXCLUDED.liveness_reason "
                "WHEN EXCLUDED.liveness_checked_at IS NOT NULL AND (apply_queue.liveness_checked_at IS NULL OR EXCLUDED.liveness_checked_at > apply_queue.liveness_checked_at) THEN EXCLUDED.liveness_reason "
                "ELSE apply_queue.liveness_reason END, "
                "liveness_checked_at=CASE "
                "WHEN apply_queue.application_url IS DISTINCT FROM EXCLUDED.application_url THEN EXCLUDED.liveness_checked_at "
                "WHEN EXCLUDED.liveness_checked_at IS NOT NULL AND (apply_queue.liveness_checked_at IS NULL OR EXCLUDED.liveness_checked_at > apply_queue.liveness_checked_at) THEN EXCLUDED.liveness_checked_at "
                "ELSE apply_queue.liveness_checked_at END, "
                "approved_batch=CASE "
                "WHEN EXCLUDED.approved_batch IS NOT NULL THEN EXCLUDED.approved_batch "
                "WHEN EXCLUDED.score IS NULL OR EXCLUDED.score < (SELECT COALESCE(approval_threshold, 7) FROM fleet_config WHERE id=1) THEN NULL "
                "ELSE apply_queue.approved_batch END, updated_at=now() "
                "WHERE apply_queue.status='queued'",
                {**r, "host": host, "dk": dk, "batch": approved_batch,
                 "require_liveness": bool(require_liveness),
                 "require_eligibility": bool(require_eligibility),
                 "eligibility_status": r.get("eligibility_status"),
                 "eligibility_reason": r.get("eligibility_reason"),
                 "liveness_status": r.get("liveness_status"),
                 "liveness_reason": r.get("liveness_reason"),
                 "liveness_checked_at": r.get("liveness_checked_at"),
                 "session_required": bool(r.get("session_required")),
                 "tenant_profile_id": r.get("tenant_profile_id"),
                 "routing_required": bool(r.get("routing_required")),
                 "execution_route": r.get("execution_route"),
                 "host_policy": r.get("host_policy")},
            )
            n += cur.rowcount
    if commit:
        conn.commit()
    if diagnostics:
        return {
            "candidates": len(prepared),
            "dedup_skipped": dedup_skipped,
            "weak_dedup_skipped": len(weak_applied_keys & {dk for _, _, dk in prepared}),
            "touched": n,
        }
    return n


def approve_jobs(conn, urls, batch, *, commit=True) -> int:
    """Stamp the owner approval token on queued rows (R11 gray-zone / batch approve)."""
    with conn.cursor() as cur:
        cur.execute("UPDATE apply_queue SET approved_batch=%s, updated_at=now() "
                    "WHERE url = ANY(%s) AND status='queued' "
                    "AND score >= (SELECT COALESCE(approval_threshold, 7) FROM fleet_config WHERE id=1)",
                    (batch, list(urls)))
        n = cur.rowcount
    if commit:
        conn.commit()
    return n


def park_challenge(conn, worker_id, url, *, kind="challenge", route=None, evidence=None,
                   commit=True) -> bool:
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
            "SELECT public.fleet_worker_park('ats',%s,%s,%s,0,%s) AS ok",
            (url, worker_id, kind, Jsonb({**(evidence or {}), "challenge_route": route})),
        )
        ok = cur.fetchone()["ok"]
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
                "apply_status='challenge_skipped', apply_error='challenge_skipped', updated_at=now() "
                "WHERE url=%s AND apply_status='challenge_pending'",
                (url,),
            )
        n = cur.rowcount
        cur.execute(
            "UPDATE auth_challenge SET resolved_at=now(), outcome=%s "
            "WHERE url=%s AND resolved_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM linkedin_queue "
            "                WHERE url=%s AND apply_status='challenge_pending')",
            ("solved" if requeue else "skipped", url, url),
        )
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


def lease_compute(conn, worker_id, *, ttl_seconds=1200, sw_version=None):
    if not _has_controller_table_authority(conn, "compute_queue"):
        with conn.cursor() as cur:
            cur.execute("SELECT public.fleet_worker_lease_compute() AS job")
            row = cur.fetchone()["job"]
        conn.commit()
        return dict(row) if row else None
    if version_mismatch(conn, worker_id, sw_version):
        return None
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
    if not _has_controller_table_authority(conn, "compute_queue"):
        usage = {
            "cost_usd": cost_usd,
            "model": model,
            "provider": provider,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }
        with conn.cursor() as cur:
            cur.execute(
                "SELECT public.fleet_worker_complete_compute(%s,%s,%s,%s,%s) AS ok",
                (url, effective_task, status, Jsonb(result), Jsonb(usage)),
            )
            landed = cur.fetchone()["ok"]
        conn.commit()
        return landed
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


_SEARCH_DEADLOCK_RETRIES = 4


def _run_search_transaction_with_retry(conn, operation):
    """Retry transient PostgreSQL deadlocks in the recurring search path."""
    for attempt in range(_SEARCH_DEADLOCK_RETRIES):
        try:
            return operation()
        except DeadlockDetected:
            conn.rollback()
            if attempt == _SEARCH_DEADLOCK_RETRIES - 1:
                raise
            time.sleep(0.05 * (2**attempt))


def lease_search(conn, worker_id, *, ttl_seconds=900, sw_version=None):
    if not _has_controller_table_authority(conn, "search_tasks"):
        with conn.cursor() as cur:
            cur.execute("SELECT public.fleet_worker_lease_search() AS task")
            row = cur.fetchone()["task"]
        conn.commit()
        return dict(row) if row else None
    if version_mismatch(conn, worker_id, sw_version):
        return None
    def operation():
        with conn.cursor() as cur:
            cur.execute(_LEASE_SEARCH, {"worker": worker_id, "ttl": ttl_seconds})
            row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None

    return _run_search_transaction_with_retry(conn, operation)


def complete_search(conn, worker_id, task_id, *, result_count=0, board=None, error=None,
                    cadence_seconds=None, postings=None):
    """Mark the search done, record a scrape outcome on the board scope, and
    RE-SCHEDULE the task (status back to 'queued', next_due_at = now + cadence)."""
    if not _has_controller_table_authority(conn, "search_tasks"):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT public.fleet_worker_complete_search(%s,%s,%s) AS ok",
                (task_id, Jsonb(postings or []), error),
            )
            landed = cur.fetchone()["ok"]
        conn.commit()
        return landed

    if postings:
        push_discovered(
            conn,
            task_id=task_id,
            source_label=board,
            worker_id=worker_id,
            postings=postings,
            commit=False,
        )

    def operation():
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

    return _run_search_transaction_with_retry(conn, operation)


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
LINKEDIN_FRESH_STATUSES = ("easy_apply", "resolved_offsite")
LINKEDIN_FRESH_MAX_AGE_DAYS = 3


_LEASE_LINKEDIN = """
WITH cfg AS (SELECT linkedin_apply_mode, linkedin_canary_enabled, linkedin_canary_remaining,
                    linkedin_policy_version, approval_threshold, paused
             FROM fleet_config WHERE id=1 FOR UPDATE),
     acct AS (
       SELECT count_24h, daily_cap, last_applied_at, min_gap_seconds, breaker_state, breaker_until, halted_until
       FROM rate_governor WHERE scope_key = 'account:linkedin' FOR UPDATE
     ),
     next AS (
         SELECT q.url FROM linkedin_queue q LEFT JOIN acct a ON TRUE LEFT JOIN cfg ON TRUE
       WHERE q.status='queued' AND q.approved_batch IS NOT NULL
         AND q.score >= GREATEST(COALESCE(cfg.approval_threshold, 7), 7)
         AND q.linkedin_resolve_status = ANY(%(fresh_statuses)s)
         AND q.linkedin_resolved_at IS NOT NULL
         AND q.linkedin_resolved_at >= now() - make_interval(days => %(fresh_days)s)
         AND (
              COALESCE(cfg.linkedin_apply_mode, 'stopped') = 'steady'
              OR (
                   COALESCE(cfg.linkedin_apply_mode, 'stopped') = 'canary'
                   AND COALESCE(cfg.linkedin_canary_enabled, FALSE)
                   AND COALESCE(cfg.linkedin_canary_remaining, 0) > 0
              )
         )
         AND NOT COALESCE(cfg.paused, FALSE)
         AND q.decision_id IS NOT NULL
         AND q.policy_version = cfg.linkedin_policy_version
         AND q.decision_action = 'apply'
         AND q.qualification_verdict = 'qualified'
         AND q.qualification_score >= q.qualification_floor
         AND q.decision_expires_at > now()
         AND q.score = q.final_score
         AND (cfg.approval_threshold IS NULL OR q.final_score >= cfg.approval_threshold)
         AND EXISTS (
           SELECT 1 FROM fleet_decision_policies p
            WHERE p.policy_version = q.policy_version AND p.lane = 'linkedin'
              AND p.status IN ('canary', 'active')
         )
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
     )
UPDATE linkedin_queue q SET status='leased', lease_owner=%(worker)s,
  lease_expires_at = now() + make_interval(secs => %(ttl)s), last_attempted_at=now(), attempts=q.attempts+1, updated_at=now()
FROM next
LEFT JOIN reserve ON TRUE
WHERE q.url = next.url
RETURNING q.url, q.company, q.title, q.application_url, q.score, q.attempts;
"""


def lease_linkedin(conn, worker_id, *, public_ip, owner_ip, ttl_seconds=1200,
                   daily_cap=20, min_gap_seconds=1200, sw_version=None):
    """LinkedIn lease: ONLY from the one owner IP, serialized by the account mutex.

    ``public_ip`` is the worker's REGISTERED egress IP and ``owner_ip`` is the
    broker-trusted owner IP -- both supplied server-side by the broker, never by the
    worker. They must match or the lease is refused outright."""
    if public_ip is None or owner_ip is None or public_ip != owner_ip:
        return None  # LinkedIn never from a different IP than the owner's
    controller = _has_controller_table_authority(conn, "fleet_config")
    if controller:
        _sync_worker_lease_authority(conn, linkedin_owner_ip=owner_ip)
    with conn.cursor() as cur:
        # Ensure the account governor row EXISTS so the FOR UPDATE mutex has a row to
        # lock (otherwise concurrent leases wouldn't serialize). Preserves an existing cap.
        if controller:
            cur.execute(
                "INSERT INTO rate_governor (scope_key, daily_cap, min_gap_seconds, base_min_gap_seconds) "
                "VALUES ('account:linkedin', %s, %s, %s) ON CONFLICT (scope_key) DO NOTHING",
                (daily_cap, min_gap_seconds, min_gap_seconds),
            )
            cur.execute(
                "INSERT INTO rate_governor (scope_key, daily_cap, min_gap_seconds, base_min_gap_seconds) "
                "VALUES ('global', 100000, 0, 0) ON CONFLICT (scope_key) DO NOTHING"
            )
        cur.execute(
            "SELECT * FROM public.fleet_worker_lease_linkedin(%s,%s,%s,%s,%s)",
            (worker_id, public_ip, owner_ip, ttl_seconds, sw_version),
        )
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
  AND attempts = %(attempt)s
RETURNING url;
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
        cur.execute(
            "SELECT public.fleet_worker_requeue('linkedin',%s,%s,%s) AS landed",
            (url, worker_id, apply_error),
        )
        landed = cur.fetchone()["landed"]
        if not landed:
            conn.rollback()
            return False
    conn.commit()
    return True


def write_linkedin_result(conn, worker_id, url, *, status, apply_status=None, apply_error=None,
                          est_cost_usd=0, outcome=None, apply_channel=None, apply_external_host=None,
                          target_host=None, home_ip=None, agent=None, agent_model=None,
                          apply_duration_ms=None, machine_owner=None,
                          application_tool_calls=None, job_log_path=None,
                          transcript_digest=None, final_result_source=None,
                          result_metadata=None):
    """Record a lease-bound LinkedIn assertion and park claimed submissions.

    This never creates canonical applied state or ``applied_set``. The account
    reservation was made at lease time and is not incremented again here.
    Returns False if the lease was lost.
    """
    return _terminalize(
        conn,
        "linkedin",
        worker_id,
        url,
        status=status,
        apply_status=apply_status,
        apply_error=apply_error,
        evidence={
            "est_cost_usd": est_cost_usd,
            "asserted_outcome": outcome,
            "apply_channel": apply_channel,
            "apply_external_host": apply_external_host,
            "asserted_target_host": target_host,
            "asserted_home_ip": home_ip,
            "agent": agent,
            "agent_model": agent_model,
            "apply_duration_ms": apply_duration_ms,
            "machine_owner": machine_owner,
            "application_tool_calls": application_tool_calls,
            "job_log_path": job_log_path,
            "transcript_digest": transcript_digest,
            "final_result_source": final_result_source,
            "result_metadata": result_metadata or {},
        },
    )

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


def park_linkedin_challenge(conn, worker_id, url, *, halt_seconds, kind="challenge",
                            route=None, evidence=None, commit=True) -> bool:
    """Freeze the held linkedin_queue lease out of reclaim AND set the account halt, in ONE tx."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT public.fleet_worker_park('linkedin',%s,%s,%s,%s,%s) AS ok",
            (url, worker_id, kind, halt_seconds, Jsonb({**(evidence or {}), "challenge_route": route})),
        )
        ok = cur.fetchone()["ok"]
    if commit:
        conn.commit()
    return ok


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
    """UPSERT linkedin_queue rows with dedup_key, lane='linkedin', and approved_batch.
    Only refreshes ``queued`` rows. ``rows`` need url, company, title,
    application_url, score. Uses the SAME _dedup.dedup_key as the offsite lane
    so cross-lane dedup (applied_set) works correctly."""
    n = 0
    with conn.cursor() as cur:
        for r in rows:
            _validate_canonical_queue_row(r)
            dk = r.get("dedup_key") or _dedup.dedup_key(r.get("company"), r.get("title"))
            params = {
                **r,
                "dk": dk,
                "batch": approved_batch,
                "linkedin_resolve_status": r.get("linkedin_resolve_status"),
                "linkedin_resolved_at": r.get("linkedin_resolved_at"),
                "linkedin_resolve_error": r.get("linkedin_resolve_error"),
                "linkedin_unresolved_kind": r.get("linkedin_unresolved_kind"),
                "linkedin_next_action": r.get("linkedin_next_action"),
            }
            cur.execute(
                "INSERT INTO linkedin_queue "
                "(url, company, title, application_url, score, lane, dedup_key, approved_batch, "
                "linkedin_resolve_status, linkedin_resolved_at, linkedin_resolve_error, "
                "linkedin_unresolved_kind, linkedin_next_action, decision_id, policy_version, decision_action, qualification_verdict, qualification_score, qualification_floor, preference_score, outcome_score, final_score, decision_confidence, decision_created_at, decision_expires_at, input_hash) "
                "VALUES (%(url)s,%(company)s,%(title)s,%(application_url)s,%(score)s,'linkedin',%(dk)s,%(batch)s, "
                "%(linkedin_resolve_status)s,%(linkedin_resolved_at)s,%(linkedin_resolve_error)s, "
                "%(linkedin_unresolved_kind)s,%(linkedin_next_action)s,%(decision_id)s,%(policy_version)s,%(decision_action)s,%(qualification_verdict)s,%(qualification_score)s,%(qualification_floor)s,%(preference_score)s,%(outcome_score)s,%(final_score)s,%(decision_confidence)s,%(decision_created_at)s,%(decision_expires_at)s,%(input_hash)s) "
                "ON CONFLICT (url) DO UPDATE SET company=EXCLUDED.company, title=EXCLUDED.title, "
                "application_url=EXCLUDED.application_url, score=EXCLUDED.score, "
                "dedup_key=EXCLUDED.dedup_key, linkedin_resolve_status=EXCLUDED.linkedin_resolve_status, "
                "linkedin_resolved_at=EXCLUDED.linkedin_resolved_at, "
                "linkedin_resolve_error=EXCLUDED.linkedin_resolve_error, "
                "decision_id=EXCLUDED.decision_id, policy_version=EXCLUDED.policy_version, "
                "decision_action=EXCLUDED.decision_action, qualification_verdict=EXCLUDED.qualification_verdict, "
                "qualification_score=EXCLUDED.qualification_score, qualification_floor=EXCLUDED.qualification_floor, "
                "preference_score=EXCLUDED.preference_score, outcome_score=EXCLUDED.outcome_score, final_score=EXCLUDED.final_score, "
                "decision_confidence=EXCLUDED.decision_confidence, decision_created_at=EXCLUDED.decision_created_at, "
                "decision_expires_at=EXCLUDED.decision_expires_at, input_hash=EXCLUDED.input_hash, "
                "linkedin_unresolved_kind=EXCLUDED.linkedin_unresolved_kind, "
                "linkedin_next_action=EXCLUDED.linkedin_next_action, "
                "approved_batch=CASE "
                "WHEN EXCLUDED.approved_batch IS NOT NULL THEN EXCLUDED.approved_batch "
                "WHEN EXCLUDED.score IS NULL OR EXCLUDED.score < (SELECT GREATEST(COALESCE(approval_threshold, 7), 7) FROM fleet_config WHERE id=1) THEN NULL "
                "ELSE linkedin_queue.approved_batch END, updated_at=now() "
                "WHERE linkedin_queue.status='queued'",
                params,
            )
            n += cur.rowcount
    if commit:
        conn.commit()
    return n


def approve_linkedin_jobs(conn, urls, batch, *, commit=True) -> int:
    """Stamp the owner approval token on queued linkedin_queue rows."""
    with conn.cursor() as cur:
        cur.execute("UPDATE linkedin_queue SET approved_batch=%s, updated_at=now() "
                    "WHERE url = ANY(%s) AND status='queued' "
                    "AND score >= (SELECT GREATEST(COALESCE(approval_threshold, 7), 7) FROM fleet_config WHERE id=1) "
                    "AND linkedin_resolve_status = ANY(%s) "
                    "AND linkedin_resolved_at IS NOT NULL "
                    "AND linkedin_resolved_at >= now() - make_interval(days => %s)",
                    (batch, list(urls), list(LINKEDIN_FRESH_STATUSES), LINKEDIN_FRESH_MAX_AGE_DAYS))
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
                "apply_status='challenge_skipped', apply_error='challenge_skipped', updated_at=now() "
                "WHERE url=%s AND apply_status='challenge_pending'",
                (url,),
            )
        n = cur.rowcount
        cur.execute(
            "UPDATE auth_challenge SET resolved_at=now(), outcome=%s "
            "WHERE url=%s AND resolved_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM apply_queue "
            "                WHERE url=%s AND apply_status='challenge_pending')",
            ("solved" if requeue else "skipped", url, url),
        )
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


def challenge_metadata(row) -> dict:
    """Return challenge display metadata with deterministic URL-host fallbacks."""
    item = dict(row or {})
    inferred_fields: list[str] = []
    company = str(item.get("company") or "").strip()
    title = str(item.get("title") or "").strip()
    if not company:
        company = host_of(item.get("application_url")) or host_of(item.get("url"))
        if company:
            inferred_fields.append("company")
    item["company"] = company or None
    item["title"] = title or None
    item["metadata_inferred_fields"] = inferred_fields
    item["metadata_complete"] = bool(company and title)
    return item


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
                "SELECT url, company, title, application_url, score, updated_at "
                "FROM apply_queue WHERE apply_status='challenge_pending'")
            parked_apply = cur.fetchall()
        if lane in (None, "linkedin"):
            cur.execute(
                "SELECT url, company, title, application_url, score, updated_at "
                "FROM linkedin_queue WHERE apply_status='challenge_pending'")
            parked_linkedin = cur.fetchall()
    conn.rollback()  # read-only

    parked_by_url: dict[str, tuple[str, dict]] = {}
    for r in parked_apply:
        parked_by_url[r["url"]] = ("apply", challenge_metadata(r))
    for r in parked_linkedin:
        parked_by_url[r["url"]] = ("linkedin", challenge_metadata(r))

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
