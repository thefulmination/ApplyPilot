"""Read-only diagnosis helpers for the LAN fleet console.

No live actions are performed here. Every function receives an existing PG connection,
uses parameterized SQL, and rolls back its read transaction before returning.
"""
from __future__ import annotations

from applypilot import config


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _queue_counts(cur, table: str, *, lane: str | None = None) -> dict[str, int]:
    lane_sql, params = _lane_predicate(lane)
    cur.execute(
        f"SELECT q.status, COUNT(*) AS n FROM {table} q "
        f"WHERE TRUE {lane_sql}"
        "GROUP BY q.status",
        params,
    )
    return {r["status"]: int(r["n"]) for r in cur.fetchall()}


def _lane_predicate(lane: str | None) -> tuple[str, dict[str, str]]:
    if lane is None:
        return "", {}
    return "AND q.lane = %(lane)s ", {"lane": lane}


def _approved_count(cur, table: str, *, lane: str | None = None) -> int:
    lane_sql, params = _lane_predicate(lane)
    cur.execute(
        f"SELECT COUNT(*) AS n FROM {table} q "
        "WHERE q.status='queued' AND q.approved_batch IS NOT NULL "
        f"{lane_sql}",
        params,
    )
    return int(cur.fetchone()["n"])


def _dedup_blocked_count(cur, table: str, *, lane: str | None = None) -> int:
    lane_sql, params = _lane_predicate(lane)
    cur.execute(
        f"SELECT COUNT(*) AS n FROM {table} q "
        "WHERE q.status='queued' AND q.approved_batch IS NOT NULL "
        f"{lane_sql}"
        "AND EXISTS (SELECT 1 FROM applied_set a WHERE a.dedup_key = q.dedup_key)",
        params,
    )
    return int(cur.fetchone()["n"])


def _leaseable_count(
    cur,
    table: str,
    *,
    lane: str | None = None,
    canary_column: str | None = None,
    canary_enabled_column: str | None = None,
    ats_guards: bool = False,
    linkedin_account_guards: bool = False,
) -> int:
    lane_sql, params = _lane_predicate(lane)
    canary_predicate = ""
    if canary_column and canary_enabled_column:
        canary_predicate = (
            f"AND (NOT COALESCE(cfg.{canary_enabled_column}, FALSE) "
            f"     OR COALESCE(cfg.{canary_column}, 0) > 0) "
        )
    ats_predicate = ""
    if ats_guards:
        blocked_names, blocked_pats = config.load_blocked_companies()
        params.update({"blocked_names": list(blocked_names), "blocked_pats": blocked_pats})
        ats_predicate = (
            "AND NOT COALESCE(cfg.paused, FALSE) "
            "AND NOT COALESCE(cfg.ats_paused, FALSE) "
            "AND (COALESCE(cfg.spend_cap_usd, 0) <= 0 "
            "     OR (SELECT COALESCE(SUM(est_cost_usd), 0) FROM apply_queue) < cfg.spend_cap_usd) "
            "AND (glob.count_24h IS NULL OR glob.count_24h < glob.daily_cap) "
            "AND ( "
            "  NOT EXISTS (SELECT 1 FROM active_home) "
            "  OR EXISTS ( "
            "    SELECT 1 FROM active_home ah "
            "    LEFT JOIN rate_governor home ON home.scope_key = 'home_ip:' || ah.home_ip "
            "    WHERE COALESCE(home.breaker_state, 'ok') != 'demoted' "
            "      AND COALESCE(NOT (home.breaker_state = 'paused' "
            "                        AND COALESCE(home.breaker_until, 'infinity'::timestamptz) >= now()), TRUE) "
            "      AND (home.count_24h IS NULL OR home.count_24h < home.daily_cap) "
            "  ) "
            ") "
            "AND COALESCE(host.breaker_state, 'ok') != 'demoted' "
            "AND COALESCE(NOT (host.breaker_state = 'paused' "
            "                  AND COALESCE(host.breaker_until, 'infinity'::timestamptz) >= now()), TRUE) "
            "AND COALESCE(host.count_24h, 0) < COALESCE(host.daily_cap, 2000000000) "
            "AND COALESCE(host.doctor_skip_until, '-infinity'::timestamptz) < now() "
            "AND NOT ( "
            "  LOWER(TRIM(COALESCE(q.company,''))) = ANY(%(blocked_names)s) "
            "  OR q.url ILIKE ANY(%(blocked_pats)s) "
            "  OR COALESCE(q.application_url,'') ILIKE ANY(%(blocked_pats)s) "
            ") "
            "AND (COALESCE(host.last_applied_at, host.last_attempt_at) IS NULL "
            "     OR COALESCE(host.last_applied_at, host.last_attempt_at) < now() - make_interval(secs => "
            "          GREATEST(COALESCE(host.min_gap_seconds, 90), COALESCE(host.doctor_min_gap_floor, 0)) * 1.4)) "
        )
    account_join = ""
    account_predicate = ""
    if linkedin_account_guards:
        account_join = "LEFT JOIN rate_governor acct ON acct.scope_key = 'account:linkedin' "
        account_predicate = (
            "AND EXISTS (SELECT 1 FROM linkedin_owner_context WHERE owner_ip_ready) "
            "AND (acct.halted_until IS NULL OR acct.halted_until < now()) "
            "AND (acct.count_24h IS NULL OR acct.count_24h < acct.daily_cap) "
            "AND COALESCE(acct.breaker_state, 'ok') != 'demoted' "
            "AND COALESCE(NOT (acct.breaker_state = 'paused' "
            "                  AND COALESCE(acct.breaker_until, 'infinity'::timestamptz) >= now()), TRUE) "
            "AND (acct.last_applied_at IS NULL "
            "     OR acct.last_applied_at < now() - make_interval(secs => COALESCE(acct.min_gap_seconds, 1200))) "
        )
    cur.execute(
        "WITH cfg AS (SELECT * FROM fleet_config WHERE id=1), "
        "active_home AS ("
        "  SELECT DISTINCT home_ip FROM worker_heartbeat "
        "  WHERE role = 'apply' AND home_ip IS NOT NULL AND last_beat >= now() - interval '150 seconds'"
        "), "
        "linkedin_owner_context AS ("
        "  SELECT bool_or(w.public_ip IS NOT NULL AND wh.home_ip IS NOT NULL AND w.public_ip = wh.home_ip) AS owner_ip_ready "
        "  FROM worker_heartbeat wh "
        "  LEFT JOIN workers w ON w.worker_id = wh.worker_id "
        "  WHERE wh.role = 'linkedin' AND wh.last_beat >= now() - interval '150 seconds'"
        ") "
        f"SELECT COUNT(*) AS n FROM {table} q "
        "CROSS JOIN cfg "
        "LEFT JOIN rate_governor glob ON glob.scope_key = 'global' "
        "LEFT JOIN rate_governor host ON host.scope_key = 'host:' || COALESCE(q.target_host, q.apply_domain) "
        f"{account_join}"
        "WHERE q.status='queued' AND q.approved_batch IS NOT NULL "
        f"{lane_sql}"
        f"{canary_predicate}"
        f"{ats_predicate}"
        f"{account_predicate}"
        "AND NOT EXISTS (SELECT 1 FROM applied_set a WHERE a.dedup_key = q.dedup_key)",
        params,
    )
    return int(cur.fetchone()["n"])


def _linkedin_owner_context(cur) -> dict[str, bool]:
    cur.execute(
        "SELECT COUNT(*) AS n, "
        "       COALESCE(bool_or(w.public_ip IS NOT NULL AND wh.home_ip IS NOT NULL "
        "                        AND w.public_ip = wh.home_ip), FALSE) AS ready "
        "FROM worker_heartbeat wh "
        "LEFT JOIN workers w ON w.worker_id = wh.worker_id "
        "WHERE wh.role = 'linkedin' AND wh.last_beat >= now() - interval '150 seconds'"
    )
    row = cur.fetchone() or {}
    return {
        "owner_ip_context_known": int(row.get("n") or 0) > 0,
        "owner_ip_ready": bool(row.get("ready")),
    }


def queue_diagnosis(conn) -> dict:
    """Return queue eligibility and a plain-English fleet state.

    This intentionally starts with the high-signal guards that explain the current
    fleet confusion: queued, approved, leaseable, dedup-blocked, and canary exhaustion.
    Later tasks add host/governor/browser/recommendation detail on top of this shape.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT paused, ats_paused, canary_enabled, canary_remaining, "
                "linkedin_canary_enabled, linkedin_canary_remaining, spend_cap_usd "
                "FROM fleet_config WHERE id=1"
            )
            cfg = cur.fetchone() or {}

            ats_depth = _queue_counts(cur, "apply_queue", lane="ats")
            li_depth = _queue_counts(cur, "linkedin_queue")
            linkedin_owner = _linkedin_owner_context(cur)

            ats = {
                "queued": ats_depth.get("queued", 0),
                "leased": ats_depth.get("leased", 0),
                "applied": ats_depth.get("applied", 0),
                "failed": ats_depth.get("failed", 0),
                "blocked": ats_depth.get("blocked", 0),
                "crash_unconfirmed": ats_depth.get("crash_unconfirmed", 0),
                "approved": _approved_count(cur, "apply_queue", lane="ats"),
                "dedup_blocked": _dedup_blocked_count(cur, "apply_queue", lane="ats"),
                "leaseable": _leaseable_count(
                    cur,
                    "apply_queue",
                    lane="ats",
                    canary_enabled_column="canary_enabled",
                    canary_column="canary_remaining",
                    ats_guards=True,
                ),
                "canary_enabled": bool(cfg.get("canary_enabled")),
                "canary_remaining": cfg.get("canary_remaining"),
                "canary_exhausted": bool(cfg.get("canary_enabled"))
                and int(cfg.get("canary_remaining") or 0) <= 0,
                "paused": bool(cfg.get("paused")),
                "ats_paused": bool(cfg.get("ats_paused")),
            }
            linkedin = {
                "queued": li_depth.get("queued", 0),
                "leased": li_depth.get("leased", 0),
                "applied": li_depth.get("applied", 0),
                "failed": li_depth.get("failed", 0),
                "approved": _approved_count(cur, "linkedin_queue"),
                "dedup_blocked": _dedup_blocked_count(cur, "linkedin_queue"),
                "leaseable": _leaseable_count(
                    cur,
                    "linkedin_queue",
                    canary_enabled_column="linkedin_canary_enabled",
                    canary_column="linkedin_canary_remaining",
                    linkedin_account_guards=True,
                ),
                "canary_enabled": bool(cfg.get("linkedin_canary_enabled")),
                "canary_remaining": cfg.get("linkedin_canary_remaining"),
                "canary_exhausted": bool(cfg.get("linkedin_canary_enabled"))
                and int(cfg.get("linkedin_canary_remaining") or 0) <= 0,
                **linkedin_owner,
            }
    finally:
        conn.rollback()

    if ats["paused"]:
        state = {
            "code": "paused",
            "severity": "halted",
            "reason": "Fleet is paused by the shared kill switch.",
        }
    elif ats["ats_paused"]:
        state = {"code": "ats_paused", "severity": "halted", "reason": "ATS lane is paused."}
    elif ats["canary_exhausted"]:
        state = {
            "code": "ats_canary_exhausted",
            "severity": "halted",
            "reason": "ATS canary is exhausted.",
        }
    elif ats["leaseable"] > 0:
        state = {
            "code": "ready_to_apply",
            "severity": "ok",
            "reason": "Leaseable ATS jobs are available.",
        }
    elif ats["approved"] > 0 and ats["dedup_blocked"] == ats["approved"]:
        state = {
            "code": "idle_no_leasable_jobs",
            "severity": "warn",
            "reason": "Approved queued ATS rows are already protected by applied_set dedup guards.",
        }
    else:
        state = {
            "code": "idle_no_leasable_jobs",
            "severity": "warn",
            "reason": "No leaseable ATS jobs are available.",
        }

    return {"state": state, "ats": ats, "linkedin": linkedin}


def browser_health(conn) -> dict:
    from applypilot.fleet.console_browser_health import summarize_worker_logs

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT worker_id, machine_owner, last_error, recent_log "
                "FROM worker_heartbeat WHERE role='apply' ORDER BY worker_id"
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.rollback()
    return summarize_worker_logs(rows)


def operational_rollups(conn) -> dict:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT worker_id, machine_owner, role, state, last_beat "
                "FROM worker_heartbeat ORDER BY machine_owner NULLS LAST, worker_id"
            )
            worker_rows = cur.fetchall()
            cur.execute(
                "SELECT COALESCE(target_host, apply_domain, '(unknown)') AS host, "
                "COUNT(*) AS total, "
                "COUNT(*) FILTER (WHERE status='applied') AS applied, "
                "COUNT(*) FILTER (WHERE status='failed') AS failed, "
                "COUNT(*) FILTER (WHERE apply_status='challenge_pending') AS challenges "
                "FROM apply_queue GROUP BY 1 ORDER BY total DESC, host ASC LIMIT 25"
            )
            host_rows = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*) FILTER (WHERE status='applied' AND updated_at > now() - interval '1 hour') AS applied_1h, "
                "COUNT(*) FILTER (WHERE status='applied' AND updated_at > now() - interval '24 hours') AS applied_24h, "
                "MAX(updated_at) FILTER (WHERE status='applied') AS last_apply_at "
                "FROM apply_queue"
            )
            throughput = cur.fetchone() or {}
            cur.execute(
                "SELECT worker_id, COUNT(*) AS total, "
                "COUNT(*) FILTER (WHERE status='applied') AS applied, "
                "COUNT(*) FILTER (WHERE status='failed') AS failed, "
                "COUNT(*) FILTER (WHERE status='crash_unconfirmed') AS crash_unconfirmed, "
                "COALESCE(SUM(est_cost_usd),0) AS cost_usd "
                "FROM apply_queue WHERE worker_id IS NOT NULL "
                "GROUP BY worker_id ORDER BY applied DESC, total DESC, worker_id LIMIT 50"
            )
            worker_cmp = cur.fetchall()
    finally:
        conn.rollback()

    machines: dict[str, dict] = {}
    for row in worker_rows:
        machine = row["machine_owner"] or "(unknown)"
        m = machines.setdefault(machine, {
            "workers": 0,
            "roles": {},
            "last_beat": None,
            "states": {},
        })
        m["workers"] += 1
        m["roles"][row["role"]] = m["roles"].get(row["role"], 0) + 1
        m["states"][row["state"]] = m["states"].get(row["state"], 0) + 1
        if m["last_beat"] is None or row["last_beat"] > m["last_beat"]:
            m["last_beat"] = row["last_beat"]
    for machine in machines.values():
        machine["last_beat"] = _iso(machine["last_beat"])

    applied_1h = int(throughput.get("applied_1h") or 0)
    applied_24h = int(throughput.get("applied_24h") or 0)
    return {
        "machines": machines,
        "host_quality": [{
            "host": r["host"],
            "total": int(r["total"] or 0),
            "applied": int(r["applied"] or 0),
            "failed": int(r["failed"] or 0),
            "challenges": int(r["challenges"] or 0),
        } for r in host_rows],
        "throughput": {
            "applied_1h": applied_1h,
            "applied_24h": applied_24h,
            "estimated_applies_per_hour": applied_1h if applied_1h > 0 else round(applied_24h / 24, 2),
        },
        "daily_goal": {
            "configured": False,
            "target": None,
            "applied_today": applied_24h,
            "remaining": None,
        },
        "worker_comparison": [{
            "worker_id": r["worker_id"],
            "total": int(r["total"] or 0),
            "applied": int(r["applied"] or 0),
            "failed": int(r["failed"] or 0),
            "crash_unconfirmed": int(r["crash_unconfirmed"] or 0),
            "cost_usd": float(r["cost_usd"] or 0),
        } for r in worker_cmp],
        "freshness": {
            "last_apply_at": _iso(throughput.get("last_apply_at")),
        },
    }


def recommendations_from(queue: dict, browser: dict) -> list[dict]:
    recs: list[dict] = []
    ats = queue["ats"]
    linkedin = queue["linkedin"]
    if ats["approved"] > 0 and ats["leaseable"] == 0 and ats["dedup_blocked"] == ats["approved"]:
        recs.append({
            "code": "reconcile_dedup_blocked_queue",
            "severity": "warn",
            "lane": "ats",
            "title": "Queued ATS rows are dedup-blocked",
            "reason": f"{ats['dedup_blocked']} approved queued ATS rows are already in applied_set.",
            "action_type": "manual_runbook",
            "command": "Run a read-only queue reconcile report before mutating any rows.",
        })
    if linkedin["queued"] > 0 and linkedin["canary_exhausted"]:
        recs.append({
            "code": "rearm_linkedin_canary",
            "severity": "info",
            "lane": "linkedin",
            "title": "LinkedIn canary is exhausted",
            "reason": "LinkedIn has queued rows but linkedin_canary_remaining is zero.",
            "action_type": "manual_operator",
            "command": "Use the LinkedIn lane runbook to re-arm a small canary if you want LinkedIn active.",
        })
    if browser["counts"].get("browser_service_unavailable") or browser["counts"].get(
        "browser_backend_crashed"
    ) or browser["counts"].get(
        "browser_server_unavailable"
    ):
        recs.append({
            "code": "restart_browser_backend",
            "severity": "warn",
            "lane": "ats",
            "title": "Browser backend failures detected",
            "reason": "Recent worker logs include browser backend crash or server unavailable failures.",
            "action_type": "manual_machine",
            "command": "Restart the affected machine's browser/apply worker stack, then verify heartbeat.",
        })
    if not recs:
        recs.append({
            "code": "no_immediate_action",
            "severity": "ok",
            "lane": "fleet",
            "title": "No immediate action required",
            "reason": "No high-priority console diagnosis fired.",
            "action_type": "none",
            "command": "",
        })
    return recs


def full_diagnosis(conn) -> dict:
    queue = queue_diagnosis(conn)
    browser = browser_health(conn)
    rollups = operational_rollups(conn)
    return {
        "queue": queue,
        "browser": browser,
        "rollups": rollups,
        "recommendations": recommendations_from(queue, browser),
    }
