from __future__ import annotations

from types import SimpleNamespace

import pytest
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from applypilot.brain.lifecycle import (
    BrainLifecycleError,
    _REQUIRED_RELATIONS,
    arm_canary,
    authority_status,
    stop_canary,
    transition_policy,
)
from applypilot.brain import schema as brain_schema


class _Result:
    def __init__(self, *, one=None, all_rows=None):
        self._one = one
        self._all = all_rows or []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class _TransitionConnection:
    def __init__(self, *, lifecycle="draft", lane="ats", missing=()):
        self.lifecycle = lifecycle
        self.lane = lane
        self.missing = set(missing)
        self.commits = 0
        self.rollbacks = 0
        self.fleet_policy_status = None
        self.active_policy = None
        self.prior_ats_pause_source = None
        self.capabilities = {"brain_policy_controller"}
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.config = {
            "paused": True,
            "ats_paused": True,
            "ats_pause_source": "test",
            "ats_apply_mode": "stopped",
            "linkedin_apply_mode": "stopped",
            "canary_enabled": False,
            "linkedin_canary_enabled": False,
            "canary_remaining": None,
            "linkedin_canary_remaining": None,
            "ats_policy_version": None,
            "linkedin_policy_version": None,
            "linkedin_owner_ip": "203.0.113.10",
            "pinned_worker_version": "test-version",
            "canary_worker_id": f"{lane}-0",
            "canary_version": None,
            "spend_cap_usd": 1,
        }

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        if "current_database()" in normalized:
            return _Result(
                one={
                    "database_name": "applypilot_staging",
                    "session_user": "brain_controller",
                    "current_user": "brain_controller",
                }
            )
        if "WITH RECURSIVE memberships(roleid)" in normalized:
            return _Result(
                one={
                    "expected": params[0] in self.capabilities,
                    "forbidden": any(role in self.capabilities for role in params[1]),
                    "extra_membership": False,
                    "unsafe_identity": False,
                    "public_create": False,
                    "mutation_authority": False,
                    "unsafe_direct_function_grant": False,
                }
            )
        if "to_regclass" in normalized:
            relation = params[0].removeprefix("public.")
            return _Result(one={"relation": None if relation in self.missing else params[0]})
        if "brain_controller_transition_policy" in normalized:
            predecessor = {
                "validated": "draft",
                "canary": "validated",
                "active": "canary",
                "retired": "active",
            }[params[1]]
            if params[2] is not None and params[2] != self.lane:
                raise BrainLifecycleError(f"policy belongs to {self.lane}, not {params[2]}")
            if self.lifecycle != predecessor:
                raise BrainLifecycleError(f"policy must be {predecessor}")
            if not self.config["paused"]:
                raise BrainLifecycleError("global fleet pause")
            if self.lane == "ats" and not self.config["ats_paused"]:
                raise BrainLifecycleError("ATS operator pause")
            self.lifecycle = params[1]
            if params[1] in {"validated", "canary"}:
                self.fleet_policy_status = params[1]
            return _Result(
                one={
                    "result": {
                        "policy_version": params[0],
                        "lane": self.lane,
                        "lifecycle": self.lifecycle,
                    }
                }
            )
        if "brain_controller_arm_canary" in normalized:
            if self.lifecycle != "canary" or self.fleet_policy_status != "canary":
                raise BrainLifecycleError("matching canary lifecycle policy is required")
            self.prior_ats_pause_source = self.config["ats_pause_source"]
            if params[1] == "ats":
                expected = None if params[4] else params[3]
                if self.config["ats_pause_source"] != expected:
                    raise BrainLifecycleError("pause source changed")
            if not self.config["canary_worker_id"]:
                raise BrainLifecycleError("pinned canary worker identity")
            if params[1] == "ats":
                self.config.update(
                    paused=False,
                    ats_paused=False,
                    ats_policy_version=params[0],
                    ats_apply_mode="canary",
                    canary_enabled=True,
                    canary_remaining=params[2],
                )
                candidate_url = "https://example.test/ats"
            else:
                self.config.update(
                    paused=False,
                    linkedin_policy_version=params[0],
                    linkedin_apply_mode="canary",
                    linkedin_canary_enabled=True,
                    linkedin_canary_remaining=params[2],
                )
                candidate_url = "https://linkedin.test/job"
            return _Result(
                one={
                    "result": {
                        "policy_version": params[0],
                        "lane": params[1],
                        "capacity": params[2],
                        "worker_id": self.config["canary_worker_id"],
                        "prior_active_policy": self.active_policy,
                        "pinned_worker_version": self.config["pinned_worker_version"],
                        "expected_worker_version": self.config["canary_version"]
                        or self.config["pinned_worker_version"],
                        "candidate_url": candidate_url,
                        "fleet_config": dict(self.config),
                    }
                }
            )
        if "brain_controller_stop_canary" in normalized:
            lane = params[0]
            armed = (
                self.config["ats_apply_mode"] == "canary" and self.config["canary_enabled"]
                if lane == "ats"
                else self.config["linkedin_apply_mode"] == "canary"
                and self.config["linkedin_canary_enabled"]
            )
            if not armed:
                raise BrainLifecycleError(f"{lane} is not the currently armed canary lane")
            candidate = self.config[
                "ats_policy_version" if lane == "ats" else "linkedin_policy_version"
            ]
            self.config.update(
                paused=True,
                ats_apply_mode="stopped",
                canary_enabled=False,
                canary_remaining=None,
                linkedin_apply_mode="stopped",
                linkedin_canary_enabled=False,
                linkedin_canary_remaining=None,
            )
            if lane == "ats":
                self.config.update(
                    ats_paused=True,
                    ats_pause_source=self.prior_ats_pause_source,
                    ats_policy_version=self.active_policy,
                )
            else:
                self.config["linkedin_policy_version"] = self.active_policy
            return _Result(
                one={
                    "result": {
                        "lane": lane,
                        "candidate_policy": candidate,
                        "restored_active_policy": self.active_policy,
                        "fleet_config": dict(self.config),
                    }
                }
            )
        if "FROM public.brain_decision_policies WHERE policy_version" in normalized:
            return _Result(
                one={
                    "policy_version": params[0],
                    "lane": self.lane,
                    "lifecycle": self.lifecycle,
                    "gate_definition_version": 1,
                    "created_at": None,
                    "validated_at": None,
                    "canary_at": None,
                    "activated_at": None,
                    "retired_at": None,
                }
            )
        if "FROM public.brain_decision_policies WHERE lane=%s AND lifecycle='active'" in normalized:
            return _Result(
                one={"policy_version": self.active_policy}
                if self.active_policy is not None else None
            )
        if "brain_transition_policy" in normalized:
            self.lifecycle = params[1]
            return _Result(one={"brain_transition_policy": None})
        if "FROM public.fleet_config WHERE id=1" in normalized:
            return _Result(one=self.config)
        if "INSERT INTO public.fleet_decision_policies" in normalized:
            self.fleet_policy_status = params[2]
            return _Result()
        if "FROM public.fleet_decision_policies WHERE policy_version" in normalized:
            return _Result(
                one={"lane": self.lane, "status": self.fleet_policy_status}
                if self.fleet_policy_status is not None else None
            )
        if "FROM public.fleet_decision_policies WHERE lane=%s AND status='active'" in normalized:
            return _Result(
                one={"policy_version": self.active_policy}
                if self.active_policy is not None else None
            )
        if "FROM public.workers w" in normalized:
            return _Result(
                one={
                    "worker_id": f"{self.lane}-0",
                    "machine_owner": "GGGTower",
                    "public_ip": "203.0.113.10",
                    "sw_version": self.config["pinned_worker_version"],
                    "last_beat": "now",
                }
            )
        if "FROM public.rate_governor" in normalized:
            return _Result(one={"configured": len(params[0])})
        if "FROM public.apply_queue q" in normalized:
            return _Result(one={"url": "https://example.test/ats", "target_host": "example.test"})
        if "FROM public.linkedin_queue q" in normalized:
            return _Result(one={"url": "https://linkedin.test/job", "target_host": "account:linkedin"})
        if "SELECT (SELECT count(*) FROM public.apply_queue" in normalized:
            return _Result(one={"ats": 0, "linkedin": 0})
        if "UPDATE public.fleet_config" in normalized:
            if "ats_apply_mode='canary'" in normalized:
                self.config.update(
                    paused=False,
                    ats_paused=False,
                    ats_policy_version=params[1],
                    ats_apply_mode="canary",
                    canary_enabled=True,
                    canary_remaining=params[2],
                    linkedin_apply_mode="stopped",
                    linkedin_canary_enabled=False,
                    linkedin_canary_remaining=None,
                )
            elif "linkedin_apply_mode='canary'" in normalized:
                self.config.update(
                    paused=False,
                    linkedin_policy_version=params[0],
                    linkedin_apply_mode="canary",
                    linkedin_canary_enabled=True,
                    linkedin_canary_remaining=params[1],
                    ats_apply_mode="stopped",
                    canary_enabled=False,
                    canary_remaining=None,
                )
            elif "canonical_canary_stopped" in normalized:
                self.config.update(
                    paused=True,
                    ats_paused=True,
                    ats_policy_version=params[0],
                    ats_apply_mode="stopped",
                    canary_enabled=False,
                    canary_remaining=None,
                    linkedin_apply_mode="stopped",
                    linkedin_canary_enabled=False,
                    linkedin_canary_remaining=None,
                )
            else:
                self.config.update(
                    paused=True,
                    linkedin_apply_mode="stopped",
                    linkedin_canary_enabled=False,
                    linkedin_canary_remaining=None,
                    ats_apply_mode="stopped",
                    canary_enabled=False,
                    canary_remaining=None,
                )
            return _Result()
        raise AssertionError(f"unexpected SQL: {normalized}")

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_transition_requires_brain_and_fleet_in_same_database():
    conn = _TransitionConnection(missing={"fleet_config"})

    with pytest.raises(BrainLifecycleError, match="not a unified brain/fleet authority"):
        transition_policy(conn, "ats-v7", "validated")

    assert conn.lifecycle == "draft"
    assert conn.commits == 0
    assert conn.rollbacks == 1


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        ("draft", "validated"),
        ("validated", "canary"),
        ("canary", "active"),
        ("active", "retired"),
    ],
)
def test_transition_advances_exactly_one_postgres_step(current, requested):
    conn = _TransitionConnection(lifecycle=current)

    result = transition_policy(conn, "ats-v7", requested, expected_lane="ats")

    assert result["lifecycle"] == requested
    assert conn.lifecycle == requested
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_transition_refuses_skipped_lifecycle_and_lane_mismatch():
    conn = _TransitionConnection(lifecycle="draft", lane="linkedin")

    with pytest.raises(BrainLifecycleError, match="belongs to linkedin"):
        transition_policy(conn, "linkedin-v7", "validated", expected_lane="ats")
    with pytest.raises(BrainLifecycleError, match="must be canary"):
        transition_policy(conn, "linkedin-v7", "active", expected_lane="linkedin")

    assert conn.lifecycle == "draft"
    assert conn.commits == 0


def test_transition_requires_global_and_lane_pause_controls():
    conn = _TransitionConnection(lifecycle="draft")
    conn.config["paused"] = False

    with pytest.raises(BrainLifecycleError, match="global fleet pause"):
        transition_policy(conn, "ats-v7", "validated", expected_lane="ats")

    conn.config["paused"] = True
    conn.config["ats_paused"] = False
    with pytest.raises(BrainLifecycleError, match="ATS operator pause"):
        transition_policy(conn, "ats-v7", "validated", expected_lane="ats")


def test_canary_arm_requires_staged_policy_and_opens_only_requested_lane():
    conn = _TransitionConnection(lifecycle="canary", lane="ats")
    conn.fleet_policy_status = "canary"

    result = arm_canary(
        conn,
        "ats-v7",
        "ats",
        20,
        expected_ats_pause_source="test",
    )

    assert result["worker_id"] == "ats-0"
    assert conn.config["paused"] is False
    assert conn.config["ats_paused"] is False
    assert conn.config["ats_apply_mode"] == "canary"
    assert conn.config["canary_remaining"] == 20
    assert conn.config["linkedin_apply_mode"] == "stopped"
    assert conn.commits == 1


def test_canary_stop_fails_closed_with_global_pause():
    conn = _TransitionConnection(lifecycle="canary", lane="ats")
    conn.config.update(
        paused=False,
        ats_paused=False,
        ats_apply_mode="canary",
        canary_enabled=True,
        canary_remaining=3,
        ats_policy_version="ats-v7",
    )

    result = stop_canary(conn, "ats")

    assert result["fleet_config"]["paused"] is True
    assert conn.config["ats_paused"] is True
    assert conn.config["ats_apply_mode"] == "stopped"
    assert conn.config["canary_enabled"] is False
    assert conn.config["linkedin_apply_mode"] == "stopped"
    assert conn.config["linkedin_canary_enabled"] is False


def test_lifecycle_refuses_caller_owned_transaction():
    conn = _TransitionConnection()
    conn.info.transaction_status = TransactionStatus.INTRANS

    with pytest.raises(BrainLifecycleError, match="idle connection"):
        transition_policy(conn, "ats-v7", "validated")

    assert conn.commits == 0
    assert conn.rollbacks == 0


def test_lifecycle_refuses_a_controller_that_is_also_a_schema_migrator():
    conn = _TransitionConnection()
    conn.capabilities.add("brain_schema_migrator")

    with pytest.raises(BrainLifecycleError, match="must belong only"):
        transition_policy(conn, "ats-v7", "validated")

    assert conn.rollbacks == 1


def test_canary_arm_requires_exact_ats_pause_source_and_rolls_back():
    conn = _TransitionConnection(lifecycle="canary", lane="ats")
    conn.fleet_policy_status = "canary"

    with pytest.raises(BrainLifecycleError, match="pause source changed"):
        arm_canary(
            conn,
            "ats-v7",
            "ats",
            1,
            expected_ats_pause_source="different-source",
        )

    assert conn.config["paused"] is True
    assert conn.rollbacks == 1


def test_canary_arm_requires_an_explicit_canary_worker_identity():
    conn = _TransitionConnection(lifecycle="canary", lane="ats")
    conn.fleet_policy_status = "canary"
    conn.config["canary_worker_id"] = None

    with pytest.raises(BrainLifecycleError, match="pinned canary worker identity"):
        arm_canary(
            conn,
            "ats-v7",
            "ats",
            1,
            expected_ats_pause_source="test",
        )

    assert conn.rollbacks == 1


def test_canary_arm_cannot_use_a_heartbeat_window_older_than_real_leases():
    conn = _TransitionConnection(lifecycle="canary")
    conn.fleet_policy_status = "canary"

    with pytest.raises(BrainLifecycleError, match="between 15 and 120 seconds"):
        arm_canary(
            conn,
            "ats-v1",
            "ats",
            1,
            heartbeat_max_age_seconds=121,
            expected_ats_pause_source="test",
        )

    assert conn.commits == 0
    assert conn.rollbacks == 0


def test_canary_arm_can_compare_and_set_a_null_ats_pause_source():
    conn = _TransitionConnection(lifecycle="canary", lane="ats")
    conn.fleet_policy_status = "canary"
    conn.config["ats_pause_source"] = None

    result = arm_canary(
        conn,
        "ats-v7",
        "ats",
        1,
        expected_ats_pause_source=None,
    )

    assert result["candidate_url"] == "https://example.test/ats"
    assert conn.config["ats_apply_mode"] == "canary"


def test_canary_stop_rejects_the_wrong_lane_without_mutation():
    conn = _TransitionConnection(lifecycle="canary", lane="ats")
    conn.config.update(
        paused=False,
        ats_paused=False,
        ats_apply_mode="canary",
        canary_enabled=True,
        canary_remaining=1,
        ats_policy_version="ats-v7",
    )

    with pytest.raises(BrainLifecycleError, match="not the currently armed canary"):
        stop_canary(conn, "linkedin")

    assert conn.config["ats_apply_mode"] == "canary"
    assert conn.rollbacks == 1


def test_linkedin_canary_stop_preserves_ats_pause_provenance():
    conn = _TransitionConnection(lifecycle="canary", lane="linkedin")
    conn.config.update(
        paused=False,
        ats_paused=True,
        ats_pause_source="incident_hold",
        linkedin_apply_mode="canary",
        linkedin_canary_enabled=True,
        linkedin_canary_remaining=1,
        linkedin_policy_version="linkedin-v7",
    )

    stop_canary(conn, "linkedin")

    assert conn.config["ats_paused"] is True
    assert conn.config["ats_pause_source"] == "incident_hold"

def test_unified_relation_contract_covers_both_lane_queues():
    assert set(_REQUIRED_RELATIONS) >= {
        "brain_decision_policies",
        "fleet_config",
        "apply_queue",
        "linkedin_queue",
    }


def test_authority_status_reads_integrated_disposable_postgres(fleet_db):
    status_login = "brain_status_lifecycle_test"
    with psycopg.connect(fleet_db, row_factory=dict_row) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS public.fleet_desired_state("
            "machine_owner text primary key,desired_workers integer NOT NULL,agent text,model text,"
            "generation integer,updated_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.execute(
            "DO $$ BEGIN CREATE ROLE brain_schema_migrator NOLOGIN; "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
        conn.execute(
            "DO $$ BEGIN CREATE ROLE brain_schema_verifier LOGIN; "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
        conn.execute(
            "DO $$ BEGIN CREATE ROLE brain_status_reader NOLOGIN NOINHERIT; "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
        conn.execute(
            "DO $$ BEGIN CREATE ROLE brain_policy_controller NOLOGIN NOINHERIT; "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
        conn.execute("ALTER ROLE brain_schema_verifier LOGIN")
        conn.execute("GRANT brain_schema_migrator TO postgres")
        conn.execute("GRANT USAGE,CREATE ON SCHEMA public TO brain_schema_migrator")
        conn.execute("GRANT CREATE ON DATABASE postgres TO brain_schema_migrator")
        conn.execute(
            "GRANT SELECT ON ALL TABLES IN SCHEMA public TO brain_schema_migrator WITH GRANT OPTION"
        )
        for relation in (
            "fleet_config",
            "fleet_decision_policies",
            "apply_queue",
            "linkedin_queue",
            "workers",
            "worker_heartbeat",
            "fleet_worker_principals",
            "fleet_desired_state",
            "rate_governor",
        ):
            conn.execute(
                f"GRANT SELECT,INSERT,UPDATE ON public.{relation} "
                "TO brain_schema_migrator WITH GRANT OPTION"
            )
        conn.commit()
        brain_schema.ensure_schema_v1(conn)
        conn.execute(
            "INSERT INTO public.apply_queue("
            "url,application_url,score,status,lane,lease_owner,lease_expires_at) "
            "VALUES('stale-lease-proof','https://example.test/stale',1,'failed','ats',"
            "'stale-worker',now()-interval '1 hour')"
        )
        conn.commit()
        password = conninfo_to_dict(fleet_db).get("password") or "test-only-password"
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN INHERIT PASSWORD {}").format(
                sql.Identifier(status_login),
                sql.Literal(password),
            )
        )
        conn.execute(
            sql.SQL("GRANT brain_status_reader TO {}").format(sql.Identifier(status_login))
        )
        conn.commit()

    with psycopg.connect(
        make_conninfo(fleet_db, user=status_login),
        row_factory=dict_row,
    ) as status_conn:
        result = authority_status(status_conn)

    assert result["authority"] == "postgres_staging_candidate"
    assert result["cutover_proven"] is False
    assert result["identity"]["database_name"]
    assert result["fleet_config"]["paused"] is False
    assert result["lanes"]["ats"]["queue"]["queued"] == 0
    assert result["lanes"]["ats"]["queue"]["leased_rows"] == 0
    assert result["lanes"]["ats"]["queue"]["outstanding_leases"] == 1
    assert result["lanes"]["linkedin"]["queue"]["queued"] == 0
    with psycopg.connect(fleet_db, row_factory=dict_row) as conn:
        conn.execute(
            sql.SQL("REVOKE brain_status_reader FROM {}").format(sql.Identifier(status_login))
        )
        conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(status_login)))
        conn.commit()
