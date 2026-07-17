"""Postgres-authoritative canonical policy lifecycle operations."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from psycopg.pq import TransactionStatus


class BrainLifecycleError(RuntimeError):
    """The requested operation does not satisfy the unified authority contract."""


_LIFECYCLE_PREDECESSOR = {
    "validated": "draft",
    "canary": "validated",
    "active": "canary",
    "retired": "active",
}

_UNSET = object()
_REQUIRED_RELATIONS = (
    "brain_schema_versions",
    "brain_decision_policies",
    "brain_policy_artifacts",
    "brain_policy_approvals",
    "brain_policy_release_gate_events",
    "brain_canary_lifecycle_events",
    "fleet_config",
    "fleet_decision_policies",
    "fleet_desired_state",
    "fleet_worker_principals",
    "workers",
    "worker_heartbeat",
    "rate_governor",
    "apply_result_events",
    "apply_attempts",
    "applied_set",
    "fleet_worker_blocklist",
    "apply_queue",
    "linkedin_queue",
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _require_idle(conn) -> None:
    """Refuse to adopt or commit a transaction owned by the caller."""

    info = getattr(conn, "info", None)
    status = getattr(info, "transaction_status", None)
    if status != TransactionStatus.IDLE:
        raise BrainLifecycleError(
            "Postgres lifecycle commands require an idle connection with no caller transaction"
        )


def _require_unified_authority(conn, *, expected_capability: str) -> dict[str, str]:
    """Prove that brain and fleet authority coexist in this exact database."""

    row = conn.execute(
        "SELECT current_database() AS database_name, "
        "session_user AS session_user, current_user AS current_user"
    ).fetchone()
    if row is None:
        raise BrainLifecycleError("could not read Postgres connection identity")
    forbidden = (
        ["brain_schema_migrator", "brain_policy_controller"]
        if expected_capability == "brain_status_reader"
        else ["brain_schema_migrator", "brain_status_reader"]
    )
    capability = conn.execute(
        """WITH RECURSIVE memberships(roleid) AS (
             SELECT oid FROM pg_roles WHERE rolname=session_user
             UNION
             SELECT membership.roleid FROM pg_auth_members membership
             JOIN memberships prior ON prior.roleid=membership.member
           )
           SELECT EXISTS(
                     SELECT 1 FROM memberships member JOIN pg_roles role ON role.oid=member.roleid
                     WHERE role.rolname=%s
                   ) AS expected,
                  EXISTS(
                    SELECT 1 FROM memberships member JOIN pg_roles role ON role.oid=member.roleid
                     WHERE role.rolname=ANY(%s)
                   ) AS forbidden,
                  EXISTS(
                    SELECT 1 FROM memberships member JOIN pg_roles role ON role.oid=member.roleid
                    WHERE role.rolname<>session_user AND role.rolname<>%s
                  ) AS extra_membership,
                  EXISTS(
                    SELECT 1 FROM pg_roles role WHERE role.rolname=session_user
                    AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                         OR role.rolreplication OR role.rolbypassrls)
                  ) AS unsafe_identity,
                  has_schema_privilege(session_user,'public','CREATE') AS public_create,
                  EXISTS(
                    SELECT 1 FROM pg_class relation JOIN pg_namespace namespace
                      ON namespace.oid=relation.relnamespace
                    WHERE namespace.nspname='public' AND relation.relkind IN ('r','p','v','m','f')
                      AND (has_table_privilege(session_user,relation.oid,
                        'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                        OR has_any_column_privilege(session_user,relation.oid,
                          'INSERT,UPDATE,REFERENCES'))
                  ) AS mutation_authority,
                  EXISTS(
                    SELECT 1 FROM pg_proc function JOIN pg_namespace namespace
                      ON namespace.oid=function.pronamespace
                    CROSS JOIN LATERAL aclexplode(function.proacl) acl
                    WHERE namespace.nspname='public'
                      AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=session_user)
                  ) AS unsafe_direct_function_grant""",
        (
            expected_capability,
            forbidden,
            expected_capability,
        ),
    ).fetchone()
    if capability is None or any(
        (
            not capability["expected"],
            capability["forbidden"],
            capability["extra_membership"],
            capability["unsafe_identity"],
            capability["public_create"],
            capability["mutation_authority"],
            capability["unsafe_direct_function_grant"],
        )
    ):
        raise BrainLifecycleError(
            f"session_user must belong only to the {expected_capability} lifecycle capability"
        )
    missing = []
    for relation in _REQUIRED_RELATIONS:
        found = conn.execute("SELECT to_regclass(%s) AS relation", (f"public.{relation}",)).fetchone()
        if found is None or found["relation"] is None:
            missing.append(relation)
    if missing:
        raise BrainLifecycleError(
            "Postgres is not a unified brain/fleet authority; missing relations: "
            + ", ".join(missing)
        )
    return {
        "database_name": str(row["database_name"]),
        "session_user": str(row["session_user"]),
        "current_user": str(row["current_user"]),
    }


def _fleet_config(conn, *, for_update: bool = False) -> dict[str, Any]:
    suffix = " FOR UPDATE" if for_update else ""
    row = conn.execute(
        "SELECT paused,ats_paused,ats_pause_source,ats_apply_mode,linkedin_apply_mode,"
        "canary_enabled,linkedin_canary_enabled,canary_remaining,linkedin_canary_remaining,"
        "ats_policy_version,linkedin_policy_version,pinned_worker_version,"
        "canary_worker_id,canary_version,approval_threshold,spend_cap_usd "
        ",linkedin_owner_ip "
        f"FROM public.fleet_config WHERE id=1{suffix}"
    ).fetchone()
    if row is None:
        raise BrainLifecycleError("unified Postgres authority has no fleet_config singleton")
    return dict(row)


def _leaseable_candidate(
    conn,
    lane: str,
    policy_version: str,
    worker_public_ip: str,
) -> dict[str, Any]:
    """Lock and return one row that satisfies the lane lease predicate now."""

    if lane == "ats":
        row = conn.execute(
            """SELECT q.url,COALESCE(q.target_host,q.apply_domain) AS target_host
               FROM public.apply_queue q
               JOIN public.fleet_config c ON c.id=1
               JOIN public.rate_governor host
                 ON host.scope_key='host:'||COALESCE(q.target_host,q.apply_domain)
               JOIN public.rate_governor home ON home.scope_key='home_ip:'||%s
               JOIN public.rate_governor glob ON glob.scope_key='global'
               WHERE q.status='queued' AND q.lane='ats' AND q.approved_batch IS NOT NULL
                 AND q.decision_id IS NOT NULL AND q.policy_version=%s
                 AND q.decision_action='apply' AND q.qualification_verdict='qualified'
                 AND q.qualification_score>=q.qualification_floor
                 AND q.decision_expires_at>now() AND q.score=q.final_score
                 AND (c.approval_threshold IS NULL OR q.final_score>=c.approval_threshold)
                 AND COALESCE(q.apply_error,'') NOT ILIKE 'requeued_by_%%'
                 AND NOT EXISTS (
                   SELECT 1 FROM public.apply_result_events prior
                   WHERE prior.queue_name='apply_queue' AND prior.url=q.url
                     AND (COALESCE(prior.application_tool_calls,0)>0
                          OR COALESCE(prior.apply_error,'') ILIKE 'requeued_by_%%'))
                 AND EXISTS (
                   SELECT 1 FROM public.fleet_decision_policies p
                   WHERE p.policy_version=q.policy_version AND p.lane='ats' AND p.status='canary')
                 AND NOT EXISTS (
                   SELECT 1 FROM public.apply_attempts a WHERE a.dedup_key=q.dedup_key
                     AND a.state IN ('submit_started','submitted_unverified'))
                 AND NOT EXISTS (SELECT 1 FROM public.applied_set a WHERE a.dedup_key=q.dedup_key)
                 AND NOT EXISTS (
                   SELECT 1 FROM public.fleet_worker_blocklist b
                   WHERE (b.kind='company' AND lower(btrim(COALESCE(q.company,'')))=b.value)
                      OR (b.kind='pattern' AND
                          (q.url ILIKE b.value OR COALESCE(q.application_url,'') ILIKE b.value)))
                 AND (COALESCE(c.spend_cap_usd,0)<=0 OR
                      (SELECT COALESCE(sum(x.cumulative_cost_usd),0) FROM public.apply_queue x)
                        < c.spend_cap_usd)
                 AND glob.count_24h<glob.daily_cap
                 AND COALESCE(glob.breaker_state,'ok') NOT IN ('paused','demoted')
                 AND home.count_24h<home.daily_cap
                 AND COALESCE(home.breaker_state,'ok')<>'demoted'
                 AND NOT (COALESCE(home.breaker_state,'ok')='paused'
                          AND COALESCE(home.breaker_until,'infinity'::timestamptz)>=now())
                 AND host.count_24h<host.daily_cap
                 AND COALESCE(host.breaker_state,'ok')<>'demoted'
                 AND NOT (COALESCE(host.breaker_state,'ok')='paused'
                          AND COALESCE(host.breaker_until,'infinity'::timestamptz)>=now())
                 AND COALESCE(host.doctor_skip_until,'-infinity'::timestamptz)<now()
                 AND (COALESCE(host.last_applied_at,host.last_attempt_at) IS NULL OR
                      COALESCE(host.last_applied_at,host.last_attempt_at)<now()-make_interval(
                        secs=>GREATEST(COALESCE(host.min_gap_seconds,90),
                                      COALESCE(host.doctor_min_gap_floor,0))))
                 AND (NOT COALESCE(q.liveness_required,FALSE) OR
                      (q.liveness_status='live' AND
                       q.liveness_checked_at>=now()-interval '15 minutes'))
                 AND (NOT COALESCE(q.eligibility_required,FALSE) OR q.eligibility_status='eligible')
                 AND (NOT COALESCE(q.routing_required,FALSE) OR q.execution_route='deterministic')
               ORDER BY q.score DESC,q.url LIMIT 1""",
            (worker_public_ip, policy_version),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT q.url,'account:linkedin' AS target_host
               FROM public.linkedin_queue q
               JOIN public.fleet_config c ON c.id=1
               JOIN public.rate_governor account ON account.scope_key='account:linkedin'
               JOIN public.rate_governor glob ON glob.scope_key='global'
               WHERE q.status='queued' AND q.lane='linkedin' AND q.approved_batch IS NOT NULL
                 AND q.score>=GREATEST(COALESCE(c.approval_threshold,7),7)
                 AND q.linkedin_resolve_status IN ('easy_apply','resolved_offsite')
                 AND q.linkedin_resolved_at>=now()-interval '3 days'
                 AND q.decision_id IS NOT NULL AND q.policy_version=%s
                 AND q.decision_action='apply' AND q.qualification_verdict='qualified'
                 AND q.qualification_score>=q.qualification_floor
                 AND q.decision_expires_at>now() AND q.score=q.final_score
                 AND EXISTS (
                   SELECT 1 FROM public.fleet_decision_policies p
                   WHERE p.policy_version=q.policy_version AND p.lane='linkedin'
                     AND p.status='canary')
                 AND (account.halted_until IS NULL OR account.halted_until<now())
                 AND account.count_24h<account.daily_cap
                 AND COALESCE(account.breaker_state,'ok')<>'demoted'
                 AND NOT (COALESCE(account.breaker_state,'ok')='paused'
                          AND COALESCE(account.breaker_until,'infinity'::timestamptz)>=now())
                 AND (account.last_applied_at IS NULL OR
                      account.last_applied_at<now()-make_interval(
                        secs=>COALESCE(account.min_gap_seconds,1200)))
                 AND glob.count_24h<glob.daily_cap
                 AND COALESCE(glob.breaker_state,'ok') NOT IN ('paused','demoted')
                 AND NOT EXISTS (SELECT 1 FROM public.applied_set d WHERE d.dedup_key=q.dedup_key)
                 AND NOT EXISTS (
                   SELECT 1 FROM public.fleet_worker_blocklist b
                   WHERE (b.kind='company' AND lower(btrim(COALESCE(q.company,'')))=b.value)
                      OR (b.kind='pattern' AND
                          (q.url ILIKE b.value OR COALESCE(q.application_url,'') ILIKE b.value)))
               ORDER BY q.score DESC,q.url LIMIT 1""",
            (policy_version,),
        ).fetchone()
    if row is None:
        raise BrainLifecycleError(
            f"{lane} canary requires at least one currently leaseable approved job"
        )
    return dict(row)


def transition_policy(
    conn,
    policy_version: str,
    requested_lifecycle: str,
    *,
    expected_lane: str | None = None,
) -> dict[str, Any]:
    """Advance one policy exactly one locked Postgres lifecycle transition."""

    predecessor = _LIFECYCLE_PREDECESSOR.get(requested_lifecycle)
    if predecessor is None:
        raise BrainLifecycleError(
            "requested lifecycle must be validated, canary, active, or retired"
        )
    _require_idle(conn)
    try:
        _require_unified_authority(conn, expected_capability="brain_policy_controller")
        row = conn.execute(
            "SELECT public.brain_controller_transition_policy(%s,%s,%s) AS result",
            (policy_version, requested_lifecycle, expected_lane),
        ).fetchone()
        if row is None or row["result"] is None:
            raise BrainLifecycleError("Postgres transition returned no lifecycle receipt")
        updated = dict(row["result"])
        if updated.get("lifecycle") != requested_lifecycle:
            raise BrainLifecycleError("Postgres transition returned the wrong lifecycle")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _json_safe(updated)


def arm_canary(
    conn,
    policy_version: str,
    lane: str,
    capacity: int,
    *,
    heartbeat_max_age_seconds: int = 90,
    expected_ats_pause_source: str | None | object = _UNSET,
) -> dict[str, Any]:
    """Open one bounded lane canary after exact policy and worker proofs pass."""

    if lane not in {"ats", "linkedin"}:
        raise BrainLifecycleError("lane must be ats or linkedin")
    if not isinstance(capacity, int) or isinstance(capacity, bool) or not 1 <= capacity <= 100:
        raise BrainLifecycleError("canary capacity must be an integer from 1 through 100")
    if not 15 <= heartbeat_max_age_seconds <= 120:
        raise BrainLifecycleError("heartbeat freshness must be between 15 and 120 seconds")
    if lane == "ats" and expected_ats_pause_source is _UNSET:
        raise BrainLifecycleError("ATS canary arm requires the exact expected ATS pause source")
    _require_idle(conn)
    try:
        _require_unified_authority(conn, expected_capability="brain_policy_controller")
        expect_null = lane == "ats" and expected_ats_pause_source is None
        source = None if expected_ats_pause_source is _UNSET else expected_ats_pause_source
        row = conn.execute(
            "SELECT public.brain_controller_arm_canary(%s,%s,%s,%s,%s,%s) AS result",
            (policy_version, lane, capacity, source, expect_null, heartbeat_max_age_seconds),
        ).fetchone()
        if row is None or row["result"] is None:
            raise BrainLifecycleError("Postgres canary arm returned no receipt")
        result = dict(row["result"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _json_safe(result)


def stop_canary(conn, lane: str) -> dict[str, Any]:
    """Fail closed by globally pausing and stopping the selected canary lane."""

    if lane not in {"ats", "linkedin"}:
        raise BrainLifecycleError("lane must be ats or linkedin")
    _require_idle(conn)
    try:
        _require_unified_authority(conn, expected_capability="brain_policy_controller")
        row = conn.execute(
            "SELECT public.brain_controller_stop_canary(%s) AS result",
            (lane,),
        ).fetchone()
        if row is None or row["result"] is None:
            raise BrainLifecycleError("Postgres canary stop returned no receipt")
        result = dict(row["result"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _json_safe(result)


def authority_status(conn) -> dict[str, Any]:
    """Return one read-only status document from a unified Postgres candidate."""

    _require_idle(conn)
    try:
        identity = _require_unified_authority(
            conn,
            expected_capability="brain_status_reader",
        )
        policies = conn.execute(
            """SELECT p.policy_version,p.lane,p.lifecycle,p.gate_definition_version,
                      count(DISTINCT a.artifact_role) AS artifact_count,
                      count(DISTINCT approval.approval_type) AS approval_count,
                      p.created_at,p.validated_at,p.canary_at,p.activated_at,p.retired_at
               FROM public.brain_decision_policies p
               LEFT JOIN public.brain_policy_artifacts a USING(policy_version)
               LEFT JOIN public.brain_policy_approvals approval USING(policy_version)
               GROUP BY p.policy_version
               ORDER BY p.lane,p.created_at,p.policy_version"""
        ).fetchall()
        config = _fleet_config(conn)
        parity = conn.execute(
            """SELECT count(*) AS passed_runs
               FROM public.brain_parity_runs run
               JOIN LATERAL (
                 SELECT event_type FROM public.brain_parity_run_events event
                 WHERE event.parity_run_id=run.parity_run_id
                 ORDER BY event.parity_run_event_id DESC LIMIT 1
               ) head ON TRUE
               WHERE head.event_type='passed'"""
        ).fetchone()

        lanes: dict[str, Any] = {}
        for lane, table, policy_column in (
            ("ats", "apply_queue", "ats_policy_version"),
            ("linkedin", "linkedin_queue", "linkedin_policy_version"),
        ):
            configured_policy = config[policy_column]
            lane_policies = [dict(row) for row in policies if row["lane"] == lane]
            brain_live = {
                row["policy_version"]: row["lifecycle"]
                for row in lane_policies
                if row["lifecycle"] in {"canary", "active"}
            }
            fleet_live_rows = conn.execute(
                "SELECT policy_version,lane,status,activated_at,retired_at "
                "FROM public.fleet_decision_policies "
                "WHERE lane=%s AND status IN ('canary','active') ORDER BY policy_version",
                (lane,),
            ).fetchall()
            fleet_live = {row["policy_version"]: row["status"] for row in fleet_live_rows}
            brain_policy = next(
                (row for row in lane_policies if row["policy_version"] == configured_policy),
                None,
            )
            fleet_policy = next(
                (dict(row) for row in fleet_live_rows if row["policy_version"] == configured_policy),
                None,
            )
            live_sets_agree = brain_live == fleet_live
            if configured_policy is None:
                policy_consistent = live_sets_agree and not brain_live
            else:
                policy_consistent = (
                    live_sets_agree
                    and brain_policy is not None
                    and fleet_policy is not None
                    and brain_policy["lane"] == lane
                    and brain_policy["lifecycle"] == fleet_policy["status"]
                )
            queue = conn.execute(
                f"""SELECT count(*) FILTER (WHERE status='queued') AS queued,
                           count(*) FILTER (WHERE status='leased') AS leased_rows,
                           count(*) FILTER (WHERE status='leased' OR lease_owner IS NOT NULL
                               OR lease_expires_at IS NOT NULL) AS outstanding_leases,
                           count(*) FILTER (WHERE status='queued' AND
                               (decision_id IS NULL OR policy_version IS NULL OR
                                decision_action IS DISTINCT FROM 'apply' OR
                                qualification_verdict IS DISTINCT FROM 'qualified' OR
                                qualification_score IS NULL OR qualification_floor IS NULL OR
                                preference_score IS NULL OR outcome_score IS NULL OR final_score IS NULL OR
                                decision_confidence IS NULL OR decision_created_at IS NULL OR
                                decision_expires_at IS NULL OR input_hash IS NULL OR btrim(input_hash)=''
                               )) AS missing_provenance,
                           count(*) FILTER (WHERE status='queued' AND
                               decision_expires_at <= now()) AS expired_decisions,
                           count(*) FILTER (WHERE status='queued' AND
                               policy_version IS DISTINCT FROM %s) AS policy_mismatch
                    FROM public.{table}""",
                (configured_policy,),
            ).fetchone()
            lanes[lane] = {
                "configured_policy": configured_policy,
                "brain_policy": brain_policy,
                "fleet_policy": fleet_policy,
                "live_brain_policies": brain_live,
                "live_fleet_policies": fleet_live,
                "fleet_policy_consistent": policy_consistent,
                "queue": dict(queue) if queue is not None else {},
                "policies": lane_policies,
            }
        result = _json_safe(
            {
                "authority": "postgres_staging_candidate",
                "cutover_proven": False,
                "historical_passed_parity_runs": (
                    int(parity["passed_runs"]) if parity is not None else 0
                ),
                "identity": identity,
                "fleet_config": dict(config),
                "lanes": lanes,
            }
        )
    except Exception:
        conn.rollback()
        raise
    conn.rollback()
    return result
