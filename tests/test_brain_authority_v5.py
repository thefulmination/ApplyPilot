from __future__ import annotations

import hashlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from applypilot.brain import schema
from test_brain_authority_v4 import HASHES, WRITER, _dsn_for, _publish, _seed_authority

pytest_plugins = ("test_brain_authority_v4",)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture
def authority_v5_pg(authority_pg):
    with psycopg.connect(authority_pg, row_factory=dict_row) as conn:
        schema.ensure_brain_schema_v5(conn)
    return authority_pg


def _artifact(conn, name: str) -> str:
    artifact_hash = _hash(name)
    conn.execute(
        "INSERT INTO public.brain_artifacts(request_id,artifact_hash,media_type,byte_length,schema_version,location) "
        "VALUES(%s,%s,'application/json',1,1,'test') ON CONFLICT DO NOTHING",
        (f"v5-{name}", artifact_hash),
    )
    return artifact_hash


def _generation(conn, *, generation: str = "generation-a", spans=("span-a",)) -> dict[str, str]:
    manifest = _artifact(conn, f"{generation}-manifest")
    close_receipt = _artifact(conn, f"{generation}-close")
    ontology_manifest = _artifact(conn, f"{generation}-ontology-manifest")
    term_digest = _hash(f"{generation}-skill-term")
    term_id = f"skill:{term_digest}"
    conn.execute(
        "SELECT public.brain_create_factual_ontology('owner-a','ontology-v1',%s)",
        (ontology_manifest,),
    )
    conn.execute(
        "SELECT public.brain_add_factual_ontology_term("
        "'owner-a','ontology-v1',%s,'has_skill','skill',%s,%s,'Canonical skill',%s)",
        (ontology_manifest, term_digest, term_id, _artifact(conn, f"{generation}-term-receipt")),
    )
    ontology_root = conn.execute(
        "SELECT public.brain_compute_factual_ontology_root('owner-a','ontology-v1') AS root"
    ).fetchone()["root"]
    conn.execute(
        "SELECT public.brain_close_factual_ontology('owner-a','ontology-v1',1,%s,%s)",
        (ontology_root, _artifact(conn, f"{generation}-ontology-close")),
    )
    conn.execute(
        "SELECT public.brain_create_factual_generation('owner-a',%s,%s,'ontology-v1',%s)",
        (generation, manifest, ontology_root),
    )
    source_hashes = {}
    for ordinal, span in enumerate(spans):
        source_hashes[span] = _artifact(conn, f"{generation}-{span}-source")
        conn.execute(
            "SELECT public.brain_add_factual_generation_member("
            "'owner-a',%s,%s,%s,'resume',%s)",
            (generation, span, source_hashes[span], ordinal),
        )
    membership_root = conn.execute(
        "SELECT public.brain_compute_factual_membership_root('owner-a',%s) AS root",
        (generation,),
    ).fetchone()["root"]
    conn.execute(
        "SELECT public.brain_close_factual_generation('owner-a',%s,%s,%s,%s)",
        (generation, len(spans), membership_root, close_receipt),
    )
    conn.commit()
    return {
        "generation": generation,
        "manifest": membership_root,
        "membership_manifest_artifact": manifest,
        "ontology_root": ontology_root,
        "ontology_version": "ontology-v1",
        "predicate": "has_skill",
        "term_id": term_id,
        **source_hashes,
    }


def _admit(
    conn,
    seeded: dict[str, str],
    *,
    approval: str,
    event: str,
    sequence: int,
    span: str = "span-a",
    action: str = "assert",
    supersedes: str | None = None,
    issued_at: datetime | None = None,
    ontology_version: str | None = None,
    predicate: str | None = None,
    term_id: str | None = None,
) -> None:
    conn.execute(
        "SELECT public.brain_admit_factual_event("
        "'owner-a',%s,%s,%s,%s,%s,%s,'resume',%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            seeded["generation"],
            span,
            approval,
            _artifact(conn, f"{approval}-receipt"),
            _hash(f"{event}-claim"),
            seeded[span],
            ontology_version or seeded["ontology_version"],
            predicate or seeded["predicate"],
            term_id or seeded["term_id"],
            event,
            _artifact(conn, f"{event}-artifact"),
            sequence,
            action,
            supersedes,
            issued_at or datetime.now(UTC) - timedelta(seconds=1),
        ),
    )


def _semantic_root(conn, generation: str = "generation-a") -> str:
    return conn.execute(
        "SELECT public.brain_compute_factual_semantic_root('owner-a',%s) AS root",
        (generation,),
    ).fetchone()["root"]


def _bound_v5_authority(
    conn,
    *,
    bind: bool = True,
    snapshot_valid_to: datetime | None = None,
) -> tuple[dict[str, object], int]:
    seeded = _generation(conn)
    _admit(conn, seeded, approval="approval-a", event="event-a", sequence=1)
    conn.execute(
        "SELECT public.brain_record_factual_assertion_coverage('owner-a','generation-a','span-a','event-a')"
    )
    conn.execute(
        "SELECT public.brain_publish_factual_snapshot("
        "'owner-a','graph-snapshot-a','generation-a',%s,%s,%s,1,now(),%s,%s)",
        (
            _semantic_root(conn),
            _artifact(conn, "coverage"),
            seeded["manifest"],
            snapshot_valid_to,
            _artifact(conn, "snapshot"),
        ),
    )
    conn.commit()
    authority = _seed_authority(conn, predecessor_deny=False)
    conn.execute("SET session_replication_role=replica")
    conn.execute(
        "UPDATE public.brain_graph_approval_receipts SET approval_state='denied',approval_artifact_hash=%s "
        "WHERE graph_approval_receipt_id=%s",
        (HASHES[4], authority["receipt_id"]),
    )
    conn.execute("SET session_replication_role=origin")
    grant_event_id = conn.execute(
        "SELECT public.brain_record_authority_epoch_event(%s,1,'granted',7,%s,NULL,'test-owner',%s) AS id",
        (authority["scope_id"], authority["incarnation"], _artifact(conn, "authority-grant-v7")),
    ).fetchone()["id"]
    approved_id = conn.execute(
        "SELECT public.brain_record_graph_approval_v5("
        "%s,7,%s,'graph-snapshot-a','approved',%s,%s,%s) AS graph_approval_receipt_id",
        (authority["scope_id"], authority["incarnation"], HASHES[3], authority["receipt_id"], HASHES[4]),
    ).fetchone()["graph_approval_receipt_id"]
    if bind:
        conn.execute(
            "SELECT public.brain_bind_factual_snapshot_approval('owner-a','graph-snapshot-a',%s)",
            (approved_id,),
        )
    conn.commit()
    authority["v5_grant_event_id"] = grant_event_id
    return authority, approved_id


def _publish_v5_candidate(conn, authority, approved_id: int, *, suffix: str) -> None:
    conn.execute(
        "SELECT public.brain_publish_v5_candidate("
        "'owner-a','campaign-a','core_fit','ats','host:example.test',7,%s,"
        "%s,%s,%s,%s,%s,%s)",
        (
            authority["incarnation"],
            f"decision-{suffix}",
            _hash(f"semantic-{suffix}"),
            HASHES[0],
            f"envelope-{suffix}",
            HASHES[1],
            approved_id,
        ),
    )


class _ReverseFetchallCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def connection(self):
        return self._cursor.connection

    def execute(self, *args, **kwargs):
        self._cursor.execute(*args, **kwargs)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return list(reversed(self._cursor.fetchall()))


def test_v5_upgrade_preserves_prior_checksums_and_supersedes_publish_acl(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT version,migration_checksum FROM public.brain_schema_versions ORDER BY version"
        ).fetchall()
        assert [row["version"] for row in rows] == [1, 2, 3, 4, 5]
        assert rows[3]["migration_checksum"] == schema._EXPECTED_V4_CHECKSUM
        assert rows[4]["migration_checksum"] == schema._EXPECTED_V5_CHECKSUM
        assert schema._schema_v5_checksum() == schema._EXPECTED_V5_CHECKSUM
        assert not conn.execute(
            "SELECT has_function_privilege('brain_candidate_writer',"
            "'public.brain_publish_v4_candidate(text,text,text,text,text,bigint,uuid,text,text,text,text,text,bigint)',"
            "'EXECUTE') AS allowed"
        ).fetchone()["allowed"]
        assert conn.execute(
            "SELECT has_function_privilege('brain_candidate_writer',"
            "'public.brain_publish_v5_candidate(text,text,text,text,text,bigint,uuid,text,text,text,text,text,bigint)',"
            "'EXECUTE') AS allowed"
        ).fetchone()["allowed"]
        conn.commit()
        schema.verify_brain_schema_v5(conn)
    with psycopg.connect(_dsn_for(authority_v5_pg, WRITER), row_factory=dict_row) as writer:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer.execute(
                "INSERT INTO public.brain_factual_generations("
                "owner_id,generation_id,membership_manifest_hash) VALUES('forged','forged',%s)",
                (_hash("forged"),),
            )


def test_v5_rejects_modified_migration_bytes_before_execution(authority_pg, monkeypatch, tmp_path):
    modified_migration = tmp_path / "schema_v5.sql"
    modified_migration.write_bytes(schema._schema_v5_bytes() + b"\n-- unauthorized mutation\n")
    monkeypatch.setattr(schema, "_SCHEMA_V5_SQL", modified_migration)

    with psycopg.connect(authority_pg, row_factory=dict_row) as conn:
        with pytest.raises(RuntimeError, match="immutable schema v5 file checksum mismatch"):
            schema.ensure_brain_schema_v5(conn)
        assert conn.execute(
            "SELECT count(*) AS count FROM public.brain_schema_versions WHERE version=5"
        ).fetchone()["count"] == 0
        assert conn.execute(
            "SELECT to_regclass('public.brain_factual_generations') AS relation"
        ).fetchone()["relation"] is None


def test_v5_upgrade_preserves_v4_revoke_and_requires_newer_epoch(authority_pg):
    with psycopg.connect(authority_pg, row_factory=dict_row) as conn:
        authority = _seed_authority(conn)
        conn.execute("SET ROLE brain_schema_migrator")
        conn.execute(
            "INSERT INTO public.brain_authority_transition_events("
            "authority_scope_id,event_type,authority_epoch,database_incarnation_id,actor_id) "
            "VALUES(%s,'revoked',7,%s,'v4-revoker')",
            (authority["scope_id"], authority["incarnation"]),
        )
        conn.execute("RESET ROLE")
        conn.commit()

        schema.ensure_brain_schema_v5(conn)
        events = conn.execute(
            "SELECT authority_epoch_event_id,event_sequence,event_type,authority_epoch,predecessor_event_id "
            "FROM public.brain_authority_epoch_events WHERE authority_scope_id=%s "
            "ORDER BY event_sequence",
            (authority["scope_id"],),
        ).fetchall()
        assert [(event["event_sequence"], event["event_type"]) for event in events] == [
            (1, "granted"),
            (2, "revoked"),
        ]
        assert events[1]["predecessor_event_id"] is not None

        receipt = _artifact(conn, "post-v4-revoke-grant")
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            conn.execute(
                "SELECT public.brain_record_authority_epoch_event(%s,3,'granted',7,%s,%s,'owner',%s)",
                (
                    authority["scope_id"],
                    authority["incarnation"],
                    events[1]["authority_epoch_event_id"],
                    receipt,
                ),
            )
        conn.rollback()

        receipt = _artifact(conn, "post-v4-revoke-newer-grant")
        conn.execute(
            "SELECT public.brain_record_authority_epoch_event(%s,3,'granted',8,%s,%s,'owner',%s)",
            (authority["scope_id"], uuid.uuid4(), events[1]["authority_epoch_event_id"], receipt),
        )
        conn.commit()


def test_v5_upgrade_preserves_populated_v4_authority_and_supersedes_legacy_approval(authority_pg):
    with psycopg.connect(authority_pg, row_factory=dict_row) as owner:
        authority = _seed_authority(owner)
        owner.execute("SET ROLE brain_schema_migrator")
        denied_receipt_id = owner.execute(
            "INSERT INTO public.brain_graph_approval_receipts("
            "authority_scope_id,authority_epoch,database_incarnation_id,graph_snapshot_id,"
            "approval_state,approval_artifact_hash,predecessor_deny_receipt_hash) "
            "VALUES(%s,7,%s,'graph-snapshot-denied','denied',%s,NULL) "
            "RETURNING graph_approval_receipt_id",
            (authority["scope_id"], authority["incarnation"], HASHES[5]),
        ).fetchone()["graph_approval_receipt_id"]
        owner.execute("RESET ROLE")
        owner.commit()

    with psycopg.connect(_dsn_for(authority_pg, WRITER), row_factory=dict_row) as writer:
        assert _publish(writer, authority) == "decision-a"
        writer.commit()

    with psycopg.connect(authority_pg, row_factory=dict_row) as conn:
        before = {
            "transitions": conn.execute(
                "SELECT authority_transition_event_id,event_type,authority_epoch,database_incarnation_id,"
                "actor_id,occurred_at,created_at FROM public.brain_authority_transition_events "
                "WHERE authority_scope_id=%s ORDER BY authority_transition_event_id",
                (authority["scope_id"],),
            ).fetchall(),
            "approvals": conn.execute(
                "SELECT graph_approval_receipt_id,authority_scope_id,authority_epoch,database_incarnation_id,"
                "graph_snapshot_id,approval_state,approval_artifact_hash,predecessor_deny_receipt_hash,created_at "
                "FROM public.brain_graph_approval_receipts WHERE authority_scope_id=%s "
                "ORDER BY graph_approval_receipt_id",
                (authority["scope_id"],),
            ).fetchall(),
            "candidates": conn.execute(
                "SELECT * FROM public.brain_v4_candidate_decisions WHERE authority_scope_id=%s",
                (authority["scope_id"],),
            ).fetchall(),
            "envelopes": conn.execute(
                "SELECT envelope.* FROM public.brain_v4_decision_envelopes envelope "
                "JOIN public.brain_v4_candidate_decisions candidate "
                "ON candidate.candidate_decision_id=envelope.candidate_decision_id "
                "WHERE candidate.authority_scope_id=%s",
                (authority["scope_id"],),
            ).fetchall(),
            "consumptions": conn.execute(
                "SELECT * FROM public.brain_graph_approval_consumptions WHERE authority_scope_id=%s",
                (authority["scope_id"],),
            ).fetchall(),
        }
        assert [row["event_type"] for row in before["transitions"]] == ["granted", "candidate_published"]
        assert {row["approval_state"] for row in before["approvals"]} == {"approved", "denied"}
        assert len(before["candidates"]) == len(before["envelopes"]) == len(before["consumptions"]) == 1
        conn.commit()

        schema.ensure_brain_schema_v5(conn)

        assert conn.execute(
            "SELECT authority_transition_event_id,event_type,authority_epoch,database_incarnation_id,"
            "actor_id,occurred_at,created_at FROM public.brain_authority_transition_events "
            "WHERE authority_scope_id=%s ORDER BY authority_transition_event_id",
            (authority["scope_id"],),
        ).fetchall() == before["transitions"]
        after_approvals = conn.execute(
            "SELECT graph_approval_receipt_id,authority_scope_id,authority_epoch,database_incarnation_id,"
            "graph_snapshot_id,approval_state,approval_artifact_hash,predecessor_deny_receipt_hash,created_at "
            "FROM public.brain_graph_approval_receipts WHERE authority_scope_id=%s "
            "ORDER BY graph_approval_receipt_id",
            (authority["scope_id"],),
        ).fetchall()
        assert after_approvals == before["approvals"]
        assert conn.execute(
            "SELECT * FROM public.brain_v4_candidate_decisions WHERE authority_scope_id=%s",
            (authority["scope_id"],),
        ).fetchall() == before["candidates"]
        assert conn.execute(
            "SELECT envelope.* FROM public.brain_v4_decision_envelopes envelope "
            "JOIN public.brain_v4_candidate_decisions candidate "
            "ON candidate.candidate_decision_id=envelope.candidate_decision_id "
            "WHERE candidate.authority_scope_id=%s",
            (authority["scope_id"],),
        ).fetchall() == before["envelopes"]
        assert conn.execute(
            "SELECT * FROM public.brain_graph_approval_consumptions WHERE authority_scope_id=%s",
            (authority["scope_id"],),
        ).fetchall() == before["consumptions"]
        assert conn.execute(
            "SELECT event_sequence,event_type,authority_epoch,database_incarnation_id "
            "FROM public.brain_authority_epoch_events WHERE authority_scope_id=%s",
            (authority["scope_id"],),
        ).fetchall() == [{
            "event_sequence": 1,
            "event_type": "granted",
            "authority_epoch": 7,
            "database_incarnation_id": authority["incarnation"],
        }]
        assert conn.execute(
            "SELECT predecessor_deny_graph_approval_receipt_id FROM public.brain_graph_approval_receipts "
            "WHERE graph_approval_receipt_id=%s",
            (authority["receipt_id"],),
        ).fetchone()["predecessor_deny_graph_approval_receipt_id"] is None
        assert conn.execute(
            "SELECT approval_state FROM public.brain_graph_approval_receipts "
            "WHERE graph_approval_receipt_id=%s",
            (denied_receipt_id,),
        ).fetchone()["approval_state"] == "denied"
        assert not conn.execute(
            "SELECT has_function_privilege('brain_candidate_writer',%s,'EXECUTE') AS allowed",
            (schema._V4_PUBLISH_SIGNATURE,),
        ).fetchone()["allowed"]
        assert conn.execute(
            "SELECT has_function_privilege('brain_candidate_writer',%s,'EXECUTE') AS allowed",
            (schema._V5_PUBLISH_SIGNATURE,),
        ).fetchone()["allowed"]
        with pytest.raises(psycopg.Error, match="matching predecessor denial"):
            conn.execute(
                "SELECT public.brain_bind_factual_snapshot_approval('owner-a','graph-snapshot-a',%s)",
                (authority["receipt_id"],),
            )


def test_v5_one_snapshot_authorizes_multiple_candidates_in_one_scope(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as owner:
        authority, approved_id = _bound_v5_authority(owner)
    publish_sql = (
        "SELECT public.brain_publish_v5_candidate("
        "'owner-a','campaign-a','core_fit','ats','host:example.test',7,%s,"
        "%s,%s,%s,%s,%s,%s)"
    )
    with psycopg.connect(_dsn_for(authority_v5_pg, WRITER), row_factory=dict_row) as writer:
        for suffix, semantic_hash in (("a", "b" * 64), ("b", "c" * 64)):
            writer.execute(
                publish_sql,
                (
                    authority["incarnation"],
                    f"decision-corpus-{suffix}",
                    semantic_hash,
                    HASHES[0],
                    f"envelope-corpus-{suffix}",
                    HASHES[1],
                    approved_id,
                ),
            )
        writer.commit()
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as owner:
        assert owner.execute(
            "SELECT count(*) AS count FROM public.brain_graph_approval_consumptions "
            "WHERE graph_approval_receipt_id=%s",
            (approved_id,),
        ).fetchone()["count"] == 2
        assert owner.execute(
            "SELECT count(*) AS count FROM public.brain_v5_candidate_publication_events "
            "WHERE authority_scope_id=%s",
            (authority["scope_id"],),
        ).fetchone()["count"] == 2


def test_v5_verifier_rejects_missing_superseding_transition_index(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        conn.execute("DROP INDEX public.brain_authority_epoch_events_latest_v5")
        conn.commit()
        with pytest.raises(RuntimeError, match="explicit index contract"):
            schema.verify_brain_schema_v5(conn)


def test_v5_verifier_rejects_graph_authority_role_escalation(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        conn.execute("ALTER ROLE brain_graph_authority LOGIN")
        conn.commit()
        try:
            with pytest.raises(RuntimeError, match="fixed graph authority role"):
                schema.verify_brain_schema_v5(conn)
        finally:
            conn.execute("ALTER ROLE brain_graph_authority NOLOGIN")
            conn.commit()


def test_v5_verifier_rejects_graph_authority_privileged_membership(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        conn.execute("GRANT brain_schema_migrator TO brain_graph_authority")
        conn.commit()
        try:
            with pytest.raises(RuntimeError, match="graph authority role membership"):
                schema.verify_brain_schema_v5(conn)
        finally:
            conn.execute("REVOKE brain_schema_migrator FROM brain_graph_authority")
            conn.commit()


def test_v5_verifier_rejects_login_with_graph_and_candidate_capabilities(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        conn.execute(
            "CREATE ROLE brain_v5_conflicted_login LOGIN NOINHERIT NOSUPERUSER "
            "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        )
        conn.execute("GRANT brain_graph_authority,brain_candidate_writer TO brain_v5_conflicted_login")
        conn.commit()
        try:
            with pytest.raises(RuntimeError, match="conflicting brain capability memberships"):
                schema.verify_brain_schema_v5(conn)
        finally:
            conn.execute("DROP OWNED BY brain_v5_conflicted_login")
            conn.execute("DROP ROLE brain_v5_conflicted_login")
            conn.commit()


@pytest.mark.parametrize(
    "mutation",
    (
        "ALTER TABLE public.brain_factual_graph_snapshots "
        "ALTER COLUMN semantic_root_hash DROP NOT NULL",
        "CREATE OR REPLACE VIEW public.brain_factual_contradiction_state AS "
        "SELECT identity.owner_id,identity.contradiction_id,identity.generation_id,"
        "NULL::bigint AS contradiction_event_id,NULL::bigint AS event_sequence,"
        "'resolved'::text AS state_after,'noncritical'::text AS severity,"
        "identity.created_at AS occurred_at FROM public.brain_factual_contradictions identity WHERE false",
        "ALTER TABLE public.brain_graph_approval_receipts "
        "ALTER CONSTRAINT brain_graph_approval_receipts_v5_predecessor_fk "
        "DEFERRABLE INITIALLY DEFERRED",
        "ALTER SEQUENCE public.brain_authority_epoch_events_authority_epoch_event_id_seq INCREMENT BY 2",
        "ALTER FUNCTION public.brain_v5_sha256_text(TEXT) VOLATILE",
    ),
)
def test_v5_exact_catalog_verifier_rejects_catalog_mutation(authority_v5_pg, mutation):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        conn.execute(mutation)
        conn.commit()
        with pytest.raises(RuntimeError, match="v5 exact catalog contract mismatch"):
            schema.verify_brain_schema_v5(conn)


@pytest.mark.parametrize(
    "mutation",
    (
        "ALTER TABLE public.brain_authority_transition_events ADD COLUMN forbidden_v5_mutation TEXT",
        "ALTER TABLE public.brain_v4_decision_envelopes "
        "DROP CONSTRAINT brain_v4_decision_envelopes_candidate_decision_id_key",
        "DROP TRIGGER brain_immutable_artifact_references_immutable "
        "ON public.brain_immutable_artifact_references",
        "ALTER FUNCTION public.brain_publish_v4_candidate("
        "TEXT,TEXT,TEXT,TEXT,TEXT,BIGINT,UUID,TEXT,TEXT,TEXT,TEXT,TEXT,BIGINT) STABLE",
        "GRANT INSERT ON public.brain_immutable_artifact_references TO brain_candidate_reader",
    ),
)
def test_v5_verifier_rejects_mutated_v4_only_authority_contract(authority_v5_pg, mutation):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        conn.execute(mutation)
        conn.commit()
        with pytest.raises(
            RuntimeError,
            match="v5 exact catalog contract mismatch|invalid non-owner object ACLs",
        ):
            schema.verify_brain_schema_v5(conn)


def test_v5_catalog_hash_is_independent_of_database_result_order(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        with conn.cursor() as cursor:
            expected = schema._v5_catalog_contract_hash(cursor)
        with conn.cursor() as cursor:
            reversed_result_order = schema._v5_catalog_contract_hash(_ReverseFetchallCursor(cursor))

        assert expected == schema._PG_CATALOG_HASHES[18]["current_v5"]
        assert schema._PG_CATALOG_HASHES[18]["v5"] != schema._UNPINNED_PG18_CATALOG_HASH
        assert reversed_result_order == expected


def test_v5_concurrent_factual_admission_has_exactly_one_winner(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        seeded = _generation(conn)

    def attempt(event: str) -> bool:
        with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
            try:
                _admit(conn, seeded, approval="approval-race", event=event, sequence=1)
                conn.commit()
                return True
            except psycopg.Error:
                conn.rollback()
                return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ("event-race-a", "event-race-b")))
    assert sorted(results) == [False, True]
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        assert conn.execute(
            "SELECT count(*) AS count FROM public.brain_factual_approval_consumptions "
            "WHERE owner_id='owner-a' AND human_approval_id='approval-race'"
        ).fetchone()["count"] == 1


def test_v5_verifier_rejects_removed_ontology_mapping_check(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        constraint = conn.execute(
            "SELECT con.conname FROM pg_constraint con "
            "WHERE con.conrelid='public.brain_factual_ontology_terms'::regclass AND con.contype='c' "
            "AND pg_get_constraintdef(con.oid,true) LIKE '%work-authorization%'"
        ).fetchone()["conname"]
        conn.execute(
            psycopg.sql.SQL("ALTER TABLE public.brain_factual_ontology_terms DROP CONSTRAINT {}").format(
                psycopg.sql.Identifier(constraint)
            )
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="v5 check contract mismatch"):
            schema.verify_brain_schema_v5(conn)


def test_v5_closure_approval_sequence_and_active_supersession_guards(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        seeded = _generation(conn)
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="closed"):
            conn.execute(
                "INSERT INTO public.brain_factual_generation_members("
                "owner_id,generation_id,source_span_id,source_artifact_hash,source_class,member_ordinal) "
                "VALUES('owner-a','generation-a','late-span',%s,'resume',2)",
                (_artifact(conn, "late-source"),),
            )
        conn.rollback()
        _admit(conn, seeded, approval="approval-a", event="event-a", sequence=1)
        conn.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            _admit(conn, seeded, approval="approval-a", event="event-reuse", sequence=2)
        conn.rollback()
        with pytest.raises(psycopg.Error, match="advance by exactly one"):
            _admit(conn, seeded, approval="approval-gap", event="event-gap", sequence=3)
        conn.rollback()
        with pytest.raises(psycopg.Error, match="issued after admission"):
            _admit(
                conn,
                seeded,
                approval="approval-future",
                event="event-future",
                sequence=2,
                issued_at=datetime.now(UTC) + timedelta(minutes=1),
            )
        conn.rollback()
        _admit(
            conn,
            seeded,
            approval="approval-b",
            event="event-b",
            sequence=2,
            action="supersede",
            supersedes="event-a",
        )
        conn.commit()
        with pytest.raises(psycopg.Error, match="earlier same-span event"):
            _admit(
                conn,
                seeded,
                approval="approval-c",
                event="event-c",
                sequence=3,
                action="supersede",
                supersedes="event-a",
            )


def test_v5_ontology_membership_is_exact_and_immutable(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        seeded = _generation(conn)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _admit(
                conn,
                seeded,
                approval="approval-unknown",
                event="event-unknown",
                sequence=1,
                term_id=f"skill:{_hash('unknown')}",
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _admit(
                conn,
                seeded,
                approval="approval-predicate",
                event="event-predicate",
                sequence=1,
                predicate="has_role",
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="ontology version mismatch"):
            _admit(
                conn,
                seeded,
                approval="approval-version",
                event="event-version",
                sequence=1,
                ontology_version="ontology-v2",
            )
        conn.rollback()
        foreign_digest = _hash("foreign-owner-term")
        foreign_term = f"skill:{foreign_digest}"
        foreign_manifest = _artifact(conn, "foreign-manifest")
        conn.execute(
            "INSERT INTO public.brain_factual_ontology_manifests("
            "owner_id,ontology_version,ontology_manifest_hash) VALUES('owner-b','ontology-v1',%s)",
            (foreign_manifest,),
        )
        conn.execute(
            "INSERT INTO public.brain_factual_ontology_terms("
            "owner_id,ontology_version,ontology_manifest_hash,predicate,term_namespace,term_digest,term_id,"
            "canonical_label,term_artifact_hash) VALUES('owner-b','ontology-v1',%s,'has_skill','skill',%s,%s,"
            "'Foreign skill',%s)",
            (
                foreign_manifest,
                foreign_digest,
                foreign_term,
                _artifact(conn, "foreign-term-receipt"),
            ),
        )
        conn.commit()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _admit(
                conn,
                seeded,
                approval="approval-owner",
                event="event-owner",
                sequence=1,
                term_id=foreign_term,
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="ontology is closed"):
            conn.execute(
                "INSERT INTO public.brain_factual_ontology_terms("
                "owner_id,ontology_version,ontology_manifest_hash,predicate,term_namespace,term_digest,term_id,"
                "canonical_label,term_artifact_hash) VALUES('owner-a','ontology-v1',%s,'unknown_predicate',"
                "'skill',%s,%s,'Unknown',%s)",
                (
                    _artifact(conn, "generation-a-ontology-manifest"),
                    _hash("unknown-predicate"),
                    f"skill:{_hash('unknown-predicate')}",
                    _artifact(conn, "unknown-predicate-receipt"),
                ),
            )
        conn.rollback()
        mismatch_digest = _hash("mismatched-namespace")
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="ontology is closed"):
            conn.execute(
                "INSERT INTO public.brain_factual_ontology_terms("
                "owner_id,ontology_version,ontology_manifest_hash,predicate,term_namespace,term_digest,term_id,"
                "canonical_label,term_artifact_hash) VALUES('owner-a','ontology-v1',%s,'has_skill',"
                "'role',%s,%s,'Wrong namespace',%s)",
                (
                    _artifact(conn, "generation-a-ontology-manifest"),
                    mismatch_digest,
                    f"role:{mismatch_digest}",
                    _artifact(conn, "mismatch-receipt"),
                ),
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO public.brain_factual_ontology_manifests("
                "owner_id,ontology_version,ontology_manifest_hash) VALUES('owner-a','ontology-v1',%s)",
                (_artifact(conn, "mixed-manifest"),),
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="ontology is closed"):
            conn.execute(
                "INSERT INTO public.brain_factual_ontology_terms("
                "owner_id,ontology_version,ontology_manifest_hash,predicate,term_namespace,term_digest,term_id,"
                "canonical_label,term_artifact_hash) VALUES('owner-a','ontology-v1',%s,'has_skill','skill',%s,%s,"
                "'Conflicting label',%s)",
                (
                    _artifact(conn, "conflict-manifest"),
                    seeded["term_id"].split(":", 1)[1],
                    seeded["term_id"],
                    _artifact(conn, "conflict-term-receipt"),
                ),
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            conn.execute(
                "UPDATE public.brain_factual_ontology_terms SET canonical_label='mutated' "
                "WHERE owner_id='owner-a' AND ontology_version='ontology-v1'"
            )


def test_v5_coverage_xor_review_and_contradiction_state(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        seeded = _generation(conn, spans=("span-a", "span-b"))
        _admit(conn, seeded, approval="approval-a", event="event-a", sequence=1)
        conn.execute(
            "SELECT public.brain_record_factual_assertion_coverage('owner-a','generation-a','span-a','event-a')"
        )
        conn.commit()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="active factual event"):
            conn.execute(
                "SELECT public.brain_review_factual_exclusion("
                "'owner-a','generation-a','span-a','duplicate',%s,'reviewer',now())",
                (_artifact(conn, "duplicate-review"),),
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "SELECT public.brain_review_factual_exclusion("
                "'owner-a','generation-a','span-b','excluded',%s,'reviewer',now())",
                ("f" * 64,),
            )
        conn.rollback()
        review_hash = _artifact(conn, "span-b-review")
        conn.execute(
            "SELECT public.brain_review_factual_exclusion("
            "'owner-a','generation-a','span-b','excluded',%s,'reviewer',now())",
            (review_hash,),
        )
        contradiction_hash = _artifact(conn, "contradiction")
        conn.execute(
            "SELECT public.brain_create_factual_contradiction("
            "'owner-a','contradiction-a','generation-a',%s)",
            (contradiction_hash,),
        )
        opened_id = conn.execute(
            "SELECT public.brain_append_factual_contradiction_event("
            "'owner-a','contradiction-a',1,'opened','active','critical',NULL,NULL) AS id"
        ).fetchone()["id"]
        conn.commit()
        with pytest.raises(psycopg.Error, match="critical contradiction"):
            conn.execute(
                "SELECT public.brain_publish_factual_snapshot("
                "'owner-a','graph-snapshot-a','generation-a',%s,%s,%s,1,now(),NULL,%s)",
                (_semantic_root(conn), _artifact(conn, "coverage"), seeded["manifest"], _artifact(conn, "snapshot")),
            )
        conn.rollback()
        with pytest.raises(psycopg.Error, match="transition lineage"):
            conn.execute(
                "SELECT public.brain_append_factual_contradiction_event("
                "'owner-a','contradiction-a',2,'confirmed','active','noncritical',%s,NULL)",
                (opened_id,),
            )
        conn.rollback()
        resolution = _artifact(conn, "resolution")
        conn.execute(
            "SELECT public.brain_append_factual_contradiction_event("
            "'owner-a','contradiction-a',2,'resolved','resolved','critical',%s,%s)",
            (opened_id, resolution),
        )
        conn.execute(
            "SELECT public.brain_publish_factual_snapshot("
            "'owner-a','graph-snapshot-a','generation-a',%s,%s,%s,1,now(),NULL,%s)",
            (_semantic_root(conn), _artifact(conn, "coverage"), seeded["manifest"], _artifact(conn, "snapshot")),
        )
        conn.commit()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            conn.execute(
                "UPDATE public.brain_factual_contradiction_events SET severity='noncritical' "
                "WHERE contradiction_event_id=%s",
                (opened_id,),
            )


def test_v5_supersession_rejects_event_bound_to_immutable_assertion_coverage(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        seeded = _generation(conn)
        _admit(conn, seeded, approval="approval-covered", event="event-covered", sequence=1)
        conn.execute(
            "SELECT public.brain_record_factual_assertion_coverage("
            "'owner-a','generation-a','span-a','event-covered')"
        )

        with pytest.raises(psycopg.Error, match="immutable assertion coverage"):
            _admit(
                conn,
                seeded,
                approval="approval-superseding",
                event="event-superseding",
                sequence=2,
                action="supersede",
                supersedes="event-covered",
            )


def test_v5_admission_rejects_span_with_immutable_exclusion_coverage(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        seeded = _generation(conn)
        conn.execute(
            "SELECT public.brain_review_factual_exclusion("
            "'owner-a','generation-a','span-a','excluded',%s,'reviewer',clock_timestamp())",
            (_artifact(conn, "immutable-exclusion-review"),),
        )

        with pytest.raises(psycopg.Error, match="immutable exclusion coverage"):
            _admit(conn, seeded, approval="approval-excluded", event="event-excluded", sequence=1)


def test_v5_reviewed_outcome_never_mutates_factual_graph(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        seeded = _generation(conn)
        _admit(conn, seeded, approval="approval-outcome", event="event-outcome", sequence=1)
        root_before = _semantic_root(conn)
        high_water_before = conn.execute(
            "SELECT max(system_receipt_sequence) AS high_water FROM public.brain_graph_fact_events "
            "WHERE owner_id='owner-a' AND generation_id='generation-a'"
        ).fetchone()["high_water"]

        conn.execute(
            "INSERT INTO public.brain_jobs(job_id,source_namespace,source_job_id) "
            "VALUES('outcome-job','test','outcome-job')"
        )
        email_event_id = conn.execute(
            "INSERT INTO public.brain_email_events("
            "source_namespace,source_event_id,job_id,event_type,occurred_at) "
            "VALUES('test','outcome-email','outcome-job','reply',clock_timestamp()) "
            "RETURNING email_event_id"
        ).fetchone()["email_event_id"]
        conn.execute(
            "INSERT INTO public.brain_reviewed_outcomes("
            "source_namespace,source_event_id,job_id,email_event_id,review_status,normalized_stage,"
            "reviewer,created_at,reviewed_at,updated_at) "
            "VALUES('test','outcome-review','outcome-job',%s,'confirmed','interview','reviewer',"
            "clock_timestamp(),clock_timestamp(),clock_timestamp())",
            (email_event_id,),
        )

        assert _semantic_root(conn) == root_before
        assert conn.execute(
            "SELECT max(system_receipt_sequence) AS high_water FROM public.brain_graph_fact_events "
            "WHERE owner_id='owner-a' AND generation_id='generation-a'"
        ).fetchone()["high_water"] == high_water_before


def test_v5_binding_and_publish_reject_missing_binding_and_revocation(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as owner:
        authority, approved_id = _bound_v5_authority(owner, bind=False)
    publish_sql = (
        "SELECT public.brain_publish_v5_candidate("
        "'owner-a','campaign-a','core_fit','ats','host:example.test',7,%s,"
        "'decision-v5','b' || repeat('0',63),%s,'envelope-v5',%s,%s)"
    )
    with psycopg.connect(_dsn_for(authority_v5_pg, WRITER), row_factory=dict_row) as writer:
        with pytest.raises(psycopg.Error, match="binding"):
            writer.execute(publish_sql, (authority["incarnation"], HASHES[0], HASHES[1], approved_id))
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as owner:
        owner.execute(
            "SELECT public.brain_bind_factual_snapshot_approval('owner-a','graph-snapshot-a',%s)",
            (approved_id,),
        )
        owner.execute(
            "SELECT public.brain_record_authority_epoch_event("
            "%s,2,'revoked',7,%s,%s,'test-owner',%s)",
            (
                authority["scope_id"], authority["incarnation"],
                authority["v5_grant_event_id"], _artifact(owner, "authority-revoke-v7"),
            ),
        )
        owner.commit()
        with pytest.raises(psycopg.Error, match="latest authority event"):
            owner.execute(
                "SELECT public.brain_bind_factual_snapshot_approval('owner-a','graph-snapshot-a',%s)",
                (approved_id,),
            )
    with psycopg.connect(_dsn_for(authority_v5_pg, WRITER), row_factory=dict_row) as writer:
        with pytest.raises(psycopg.Error, match="latest authority event"):
            writer.execute(publish_sql, (authority["incarnation"], HASHES[0], HASHES[1], approved_id))


def test_v5_revoke_then_newer_epoch_regrant_can_publish(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as owner:
        authority, _ = _bound_v5_authority(owner)
        revoke_id = owner.execute(
            "SELECT public.brain_record_authority_epoch_event("
            "%s,2,'revoked',7,%s,%s,'test-owner',%s) AS id",
            (authority["scope_id"], authority["incarnation"], authority["v5_grant_event_id"],
             _artifact(owner, "regrant-revoke")),
        ).fetchone()["id"]
        owner.commit()
        with pytest.raises(psycopg.Error, match="exact predecessor and next sequence"):
            owner.execute(
                "SELECT public.brain_record_authority_epoch_event("
                "%s,4,'granted',8,%s,%s,'test-owner',%s)",
                (authority["scope_id"], authority["incarnation"], revoke_id,
                 _artifact(owner, "bad-regrant")),
            )
        owner.rollback()
        grant_v8 = owner.execute(
            "SELECT public.brain_record_authority_epoch_event("
            "%s,3,'granted',8,%s,%s,'test-owner',%s) AS id",
            (authority["scope_id"], authority["incarnation"], revoke_id,
             _artifact(owner, "regrant-v8")),
        ).fetchone()["id"]
        deny_hash = _artifact(owner, "deny-v8")
        deny_id = owner.execute(
            "SELECT public.brain_record_graph_approval_v5("
            "%s,8,%s,'graph-snapshot-a','denied',%s,NULL,NULL) AS id",
            (authority["scope_id"], authority["incarnation"], deny_hash),
        ).fetchone()["id"]
        approved_id = owner.execute(
            "SELECT public.brain_record_graph_approval_v5("
            "%s,8,%s,'graph-snapshot-a','approved',%s,%s,%s) AS id",
            (authority["scope_id"], authority["incarnation"], _artifact(owner, "approve-v8"),
             deny_id, deny_hash),
        ).fetchone()["id"]
        owner.execute(
            "SELECT public.brain_bind_factual_snapshot_approval('owner-a','graph-snapshot-a',%s)",
            (approved_id,),
        )
        owner.commit()
        assert grant_v8 > revoke_id
    with psycopg.connect(_dsn_for(authority_v5_pg, WRITER), row_factory=dict_row) as writer:
        result = writer.execute(
            "SELECT public.brain_publish_v5_candidate("
            "'owner-a','campaign-a','core_fit','ats','host:example.test',8,%s,"
            "'decision-regrant','d' || repeat('0',63),%s,'envelope-regrant',%s,%s) AS id",
            (authority["incarnation"], HASHES[0], HASHES[1], approved_id),
        ).fetchone()["id"]
        writer.commit()
        assert result == "decision-regrant"


def test_v5_computed_roots_reject_tampering_and_ignore_insert_order(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        manifest_a = _artifact(conn, "root-ontology-a")
        manifest_b = _artifact(conn, "root-ontology-b")
        terms = [
            ("has_skill", "skill", _hash("root-skill"), "Skill"),
            ("has_role", "role", _hash("root-role"), "Role"),
        ]
        for version, manifest, ordered in (
            ("root-a", manifest_a, terms),
            ("root-b", manifest_b, tuple(reversed(terms))),
        ):
            conn.execute(
                "SELECT public.brain_create_factual_ontology('root-owner',%s,%s)",
                (version, manifest),
            )
            for predicate, namespace, digest, label in ordered:
                conn.execute(
                    "SELECT public.brain_add_factual_ontology_term("
                    "'root-owner',%s,%s,%s,%s,%s,%s,%s,%s)",
                    (version, manifest, predicate, namespace, digest, f"{namespace}:{digest}",
                     label, _artifact(conn, f"root-term-{namespace}")),
                )
        conn.commit()
        root_a = conn.execute(
            "SELECT public.brain_compute_factual_ontology_root('root-owner','root-a') AS root"
        ).fetchone()["root"]
        root_b = conn.execute(
            "SELECT public.brain_compute_factual_ontology_root('root-owner','root-b') AS root"
        ).fetchone()["root"]
        assert root_a == root_b
        with pytest.raises(psycopg.Error, match="computed term membership"):
            conn.execute(
                "SELECT public.brain_close_factual_ontology('root-owner','root-a',2,%s,%s)",
                ("0" * 64, _artifact(conn, "bad-root-close")),
            )
        conn.rollback()
        for version, root in (("root-a", root_a), ("root-b", root_b)):
            conn.execute(
                "SELECT public.brain_close_factual_ontology('root-owner',%s,2,%s,%s)",
                (version, root, _artifact(conn, f"close-{version}")),
            )
        conn.commit()
        with pytest.raises(psycopg.Error, match="ontology is closed"):
            conn.execute(
                "SELECT public.brain_add_factual_ontology_term("
                "'root-owner','root-a',%s,'has_skill','skill',%s,%s,'Late',%s)",
                (manifest_a, _hash("late-root-term"), f"skill:{_hash('late-root-term')}",
                 _artifact(conn, "late-root-term-artifact")),
            )


def test_v5_ontology_root_contract_uses_c_collation_for_every_text_sort_key():
    definition = schema._schema_v5_bytes().decode("utf-8")
    assert (
        'ORDER BY term.predicate COLLATE "C",term.term_id COLLATE "C"'
        in " ".join(definition.split())
    )


def test_v5_graph_authority_is_bounded_and_separate_from_policy_controller(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        manifest = _artifact(conn, "bounded-manifest")
        conn.commit()
        conn.execute("SET ROLE brain_graph_authority")
        conn.execute(
            "SELECT public.brain_create_factual_ontology('bounded-owner','bounded-v1',%s)",
            (manifest,),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO public.brain_factual_ontology_manifests("
                "owner_id,ontology_version,ontology_manifest_hash) "
                "VALUES('forged-owner','forged-v1',%s)",
                (manifest,),
            )
        conn.rollback()
        conn.execute("SET ROLE brain_policy_controller")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "SELECT public.brain_create_factual_ontology('policy-forged','v1',%s)",
                (manifest,),
            )


def test_v5_membership_root_is_computed_and_insertion_order_independent(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        seeded = _generation(conn, generation="generation-a", spans=("span-a", "span-b"))
        membership_manifest = _artifact(conn, "generation-b-membership-manifest")
        conn.execute(
            "SELECT public.brain_create_factual_generation("
            "'owner-a','generation-b',%s,'ontology-v1',%s)",
            (membership_manifest, seeded["ontology_root"]),
        )
        for ordinal, span in reversed(tuple(enumerate(("span-a", "span-b")))):
            conn.execute(
                "SELECT public.brain_add_factual_generation_member("
                "'owner-a','generation-b',%s,%s,'resume',%s)",
                (span, seeded[span], ordinal),
            )
        conn.commit()
        root_b = conn.execute(
            "SELECT public.brain_compute_factual_membership_root('owner-a','generation-b') AS root"
        ).fetchone()["root"]
        assert root_b == seeded["manifest"]
        with pytest.raises(psycopg.Error, match="computed membership"):
            conn.execute(
                "SELECT public.brain_close_factual_generation("
                "'owner-a','generation-b',2,%s,%s)",
                ("f" * 64, _artifact(conn, "tampered-membership-close")),
            )
        conn.rollback()
        conn.execute(
            "SELECT public.brain_close_factual_generation("
            "'owner-a','generation-b',2,%s,%s)",
            (root_b, _artifact(conn, "generation-b-close")),
        )
        conn.commit()
        with pytest.raises(psycopg.Error, match="generation is closed"):
            conn.execute(
                "SELECT public.brain_add_factual_generation_member("
                "'owner-a','generation-b','late',%s,'resume',2)",
                (_artifact(conn, "late-generation-member"),),
            )


def test_v5_generation_and_scope_operations_share_lock_domains(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as owner:
        authority, approved_id = _bound_v5_authority(owner)
        transition_receipt = _artifact(owner, "lock-domain-revoke")
        owner.commit()
    with (
        psycopg.connect(authority_v5_pg, row_factory=dict_row) as locker,
        psycopg.connect(authority_v5_pg, row_factory=dict_row) as contender,
    ):
        locker.execute(
            "SELECT 1 FROM public.brain_factual_generations "
            "WHERE owner_id='owner-a' AND generation_id='generation-a' FOR UPDATE"
        )
        contender.execute("SET lock_timeout='100ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            contender.execute(
                "SELECT public.brain_review_factual_exclusion("
                "'owner-a','generation-a','span-a','blocked',%s,'reviewer',now())",
                (HASHES[0],),
            )
        contender.rollback()
        locker.rollback()
        locker.execute(
            "SELECT 1 FROM public.brain_authority_scope_state "
            "WHERE authority_scope_id=%s FOR UPDATE",
            (authority["scope_id"],),
        )
        contender.execute("SET lock_timeout='100ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            contender.execute(
                "SELECT public.brain_record_authority_epoch_event("
                "%s,2,'revoked',7,%s,%s,'test-owner',%s)",
                (authority["scope_id"], authority["incarnation"],
                 authority["v5_grant_event_id"], transition_receipt),
            )
        contender.rollback()
        locker.rollback()
    with psycopg.connect(_dsn_for(authority_v5_pg, WRITER), row_factory=dict_row) as writer:
        writer.execute("SET lock_timeout='100ms'")
        with psycopg.connect(authority_v5_pg, row_factory=dict_row) as locker:
            locker.execute(
                "SELECT 1 FROM public.brain_authority_scope_state "
                "WHERE authority_scope_id=%s FOR UPDATE",
                (authority["scope_id"],),
            )
            with pytest.raises(psycopg.errors.LockNotAvailable):
                writer.execute(
                    "SELECT public.brain_publish_v5_candidate("
                    "'owner-a','campaign-a','core_fit','ats','host:example.test',7,%s,"
                    "'decision-lock-domain','e' || repeat('0',63),%s,'envelope-lock-domain',%s,%s)",
                    (authority["incarnation"], HASHES[0], HASHES[1], approved_id),
                )
            writer.rollback()
            locker.rollback()
            locker.execute(
                "SELECT 1 FROM public.brain_factual_generations "
                "WHERE owner_id='owner-a' AND generation_id='generation-a' FOR UPDATE"
            )
            writer.execute("SET lock_timeout='100ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                _publish_v5_candidate(
                    writer,
                    authority,
                    approved_id,
                    suffix="generation-lock-domain",
                )
            writer.rollback()
            locker.rollback()


def test_v5_candidate_publication_rejects_snapshot_stale_by_later_fact_event(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as owner:
        authority, approved_id = _bound_v5_authority(owner)
        event = owner.execute(
            "SELECT event.*,member.source_artifact_hash,member.source_class "
            "FROM public.brain_graph_fact_events event "
            "JOIN public.brain_factual_generation_members member "
            "ON member.owner_id=event.owner_id AND member.generation_id=event.generation_id "
            "AND member.source_span_id=event.source_span_id "
            "WHERE event.owner_id='owner-a' AND event.event_id='event-a'"
        ).fetchone()
        owner.execute(
            "INSERT INTO public.brain_factual_approval_receipts("
            "owner_id,human_approval_id,generation_id,approval_receipt_hash,claim_projection_hash,"
            "ontology_version,predicate,term_id,source_artifact_hash,source_span_id,source_class,"
            "mutation_action,issued_at) VALUES("
            "'owner-a','legacy-late-approval','generation-a',%s,%s,%s,%s,%s,%s,%s,%s,'supersede',"
            "clock_timestamp())",
            (
                _artifact(owner, "legacy-late-approval-receipt"),
                _hash("legacy-late-claim"),
                event["ontology_version"],
                event["predicate"],
                event["term_id"],
                event["source_artifact_hash"],
                event["source_span_id"],
                event["source_class"],
            ),
        )
        owner.execute(
            "INSERT INTO public.brain_graph_fact_events("
            "owner_id,event_id,generation_id,source_span_id,human_approval_id,approval_receipt_hash,"
            "claim_projection_hash,ontology_version,predicate,term_id,event_artifact_hash,"
            "system_receipt_sequence,mutation_action,supersedes_event_id) VALUES("
            "'owner-a','legacy-late-event','generation-a',%s,'legacy-late-approval',%s,%s,%s,%s,%s,%s,"
            "2,'supersede','event-a')",
            (
                event["source_span_id"],
                _artifact(owner, "legacy-late-approval-receipt"),
                _hash("legacy-late-claim"),
                event["ontology_version"],
                event["predicate"],
                event["term_id"],
                _artifact(owner, "legacy-late-event-artifact"),
            ),
        )
        owner.commit()

    with psycopg.connect(_dsn_for(authority_v5_pg, WRITER), row_factory=dict_row) as writer:
        with pytest.raises(psycopg.Error, match="high-water"):
            _publish_v5_candidate(writer, authority, approved_id, suffix="stale-high-water")


def test_v5_candidate_publication_rejects_snapshot_stale_by_later_semantic_root(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as owner:
        authority, approved_id = _bound_v5_authority(owner)
        owner.execute(
            "SELECT public.brain_create_factual_contradiction("
            "'owner-a','later-noncritical','generation-a',%s)",
            (_artifact(owner, "later-noncritical-identity"),),
        )
        owner.execute(
            "SELECT public.brain_append_factual_contradiction_event("
            "'owner-a','later-noncritical',1,'opened','active','noncritical',NULL,NULL)"
        )
        owner.commit()

    with psycopg.connect(_dsn_for(authority_v5_pg, WRITER), row_factory=dict_row) as writer:
        with pytest.raises(psycopg.Error, match="semantic root"):
            _publish_v5_candidate(writer, authority, approved_id, suffix="stale-semantic-root")


def test_v5_candidate_publication_rechecks_later_active_critical_contradiction(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as owner:
        authority, approved_id = _bound_v5_authority(owner)
        owner.execute(
            "SELECT public.brain_create_factual_contradiction("
            "'owner-a','later-critical','generation-a',%s)",
            (_artifact(owner, "later-critical-identity"),),
        )
        owner.execute(
            "SELECT public.brain_append_factual_contradiction_event("
            "'owner-a','later-critical',1,'opened','active','critical',NULL,NULL)"
        )
        owner.commit()

    with psycopg.connect(_dsn_for(authority_v5_pg, WRITER), row_factory=dict_row) as writer:
        with pytest.raises(psycopg.Error, match="active critical contradiction"):
            _publish_v5_candidate(writer, authority, approved_id, suffix="later-critical")


def test_v5_exclusion_rejects_active_fact_future_review_and_snapshot_conflict(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        seeded = _generation(conn, spans=("span-a", "span-b"))
        _admit(conn, seeded, approval="approval-a", event="event-a", sequence=1)
        conn.commit()
        with pytest.raises(psycopg.Error, match="active factual event"):
            conn.execute(
                "SELECT public.brain_review_factual_exclusion("
                "'owner-a','generation-a','span-a','excluded',%s,'reviewer',now())",
                (_artifact(conn, "active-fact-review"),),
            )
        conn.rollback()
        with pytest.raises(psycopg.Error, match="future"):
            conn.execute(
                "SELECT public.brain_review_factual_exclusion("
                "'owner-a','generation-a','span-b','excluded',%s,'reviewer',"
                "clock_timestamp()+interval '1 hour')",
                (_artifact(conn, "future-review"),),
            )
        conn.rollback()

        conn.execute(
            "SELECT public.brain_review_factual_exclusion("
            "'owner-a','generation-a','span-b','excluded',%s,'reviewer',clock_timestamp())",
            (_artifact(conn, "valid-review"),),
        )
        conn.execute("SET session_replication_role=replica")
        conn.execute(
            "INSERT INTO public.brain_factual_generation_coverage("
            "owner_id,generation_id,source_span_id,disposition,exclusion_reason,"
            "review_receipt_hash,reviewed_at,reviewer_id) "
            "VALUES('owner-a','generation-a','span-a','exclusion','forged',%s,now(),'reviewer')",
            (_artifact(conn, "forged-active-review"),),
        )
        conn.execute("SET session_replication_role=origin")
        conn.commit()
        with pytest.raises(psycopg.Error, match="exclusion conflicts with active factual event"):
            conn.execute(
                "SELECT public.brain_publish_factual_snapshot("
                "'owner-a','snapshot-conflict','generation-a',%s,%s,%s,1,now(),NULL,%s)",
                (
                    _semantic_root(conn),
                    _artifact(conn, "conflict-coverage"),
                    seeded["manifest"],
                    _artifact(conn, "conflict-snapshot"),
                ),
            )


def test_v5_exclusion_revalidates_after_concurrent_admission(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as setup:
        seeded = _generation(setup)
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as admitting:
        _admit(admitting, seeded, approval="approval-race", event="event-race", sequence=1)

        def review_exclusion() -> str:
            with psycopg.connect(authority_v5_pg, row_factory=dict_row) as reviewer:
                reviewer.execute("SET lock_timeout='2s'")
                try:
                    reviewer.execute(
                        "SELECT public.brain_review_factual_exclusion("
                        "'owner-a','generation-a','span-a','raced',%s,'reviewer',clock_timestamp())",
                        (_artifact(reviewer, "raced-exclusion-review"),),
                    )
                    reviewer.commit()
                    return "accepted"
                except psycopg.Error as exc:
                    reviewer.rollback()
                    return str(exc)

        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(review_exclusion)
            time.sleep(0.1)
            admitting.commit()
            assert "active factual event" in result.result(timeout=3)

    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        assert conn.execute(
            "SELECT count(*) AS count FROM public.brain_factual_generation_coverage "
            "WHERE owner_id='owner-a' AND generation_id='generation-a'"
        ).fetchone()["count"] == 0


def test_v5_snapshot_creation_rejects_inactive_validity_window(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        seeded = _generation(conn)
        _admit(conn, seeded, approval="approval-a", event="event-a", sequence=1)
        conn.execute(
            "SELECT public.brain_record_factual_assertion_coverage("
            "'owner-a','generation-a','span-a','event-a')"
        )
        conn.commit()
        root = _semantic_root(conn)
        for snapshot_id, valid_from, valid_to in (
            (
                "snapshot-future",
                datetime.now(UTC) + timedelta(hours=1),
                None,
            ),
            (
                "snapshot-expired",
                datetime.now(UTC) - timedelta(hours=2),
                datetime.now(UTC) - timedelta(hours=1),
            ),
        ):
            with pytest.raises(psycopg.Error, match="currently valid"):
                conn.execute(
                    "SELECT public.brain_publish_factual_snapshot("
                    "'owner-a',%s,'generation-a',%s,%s,%s,1,%s,%s,%s)",
                    (
                        snapshot_id,
                        root,
                        _artifact(conn, f"{snapshot_id}-coverage"),
                        seeded["manifest"],
                        valid_from,
                        valid_to,
                        _artifact(conn, f"{snapshot_id}-artifact"),
                    ),
                )
            conn.rollback()


def test_v5_snapshot_expiry_is_rechecked_after_waiting_for_generation_lock(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as setup:
        seeded = _generation(setup)
        _admit(setup, seeded, approval="approval-lock", event="event-lock", sequence=1)
        setup.execute(
            "SELECT public.brain_record_factual_assertion_coverage("
            "'owner-a','generation-a','span-a','event-lock')"
        )
        coverage_hash = _artifact(setup, "lock-expiry-coverage")
        snapshot_hash = _artifact(setup, "lock-expiry-snapshot")
        semantic_root = _semantic_root(setup)
        setup.commit()

    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as blocker:
        blocker.execute(
            "SELECT 1 FROM public.brain_factual_generations "
            "WHERE owner_id='owner-a' AND generation_id='generation-a' FOR UPDATE"
        )

        def publish_waiting_snapshot() -> str:
            with psycopg.connect(authority_v5_pg, row_factory=dict_row) as publisher:
                try:
                    publisher.execute(
                        "SELECT public.brain_publish_factual_snapshot("
                        "'owner-a','snapshot-lock-expired','generation-a',%s,%s,%s,1,now(),%s,%s)",
                        (
                            semantic_root,
                            coverage_hash,
                            seeded["manifest"],
                            datetime.now(UTC) + timedelta(milliseconds=300),
                            snapshot_hash,
                        ),
                    )
                    publisher.commit()
                    return "accepted"
                except psycopg.Error as exc:
                    publisher.rollback()
                    return str(exc)

        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(publish_waiting_snapshot)
            time.sleep(0.5)
            assert not result.done()
            blocker.commit()
            assert "currently valid" in result.result(timeout=3)


def test_v5_expired_snapshot_rejected_at_binding(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as owner:
        authority, approved_id = _bound_v5_authority(
            owner,
            bind=False,
            snapshot_valid_to=datetime.now(UTC) + timedelta(milliseconds=250),
        )
        time.sleep(0.3)
        with pytest.raises(psycopg.Error, match="currently valid"):
            owner.execute(
                "SELECT public.brain_bind_factual_snapshot_approval("
                "'owner-a','graph-snapshot-a',%s)",
                (approved_id,),
            )


def test_v5_expired_snapshot_rejected_at_candidate_publication(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as owner:
        authority, approved_id = _bound_v5_authority(
            owner,
            snapshot_valid_to=datetime.now(UTC) + timedelta(seconds=2),
        )
    time.sleep(2.1)
    with psycopg.connect(_dsn_for(authority_v5_pg, WRITER), row_factory=dict_row) as writer:
        with pytest.raises(psycopg.Error, match="currently valid"):
            writer.execute(
                "SELECT public.brain_publish_v5_candidate("
                "'owner-a','campaign-a','core_fit','ats','host:example.test',7,%s,"
                "'decision-expired','e' || repeat('0',63),%s,'envelope-expired',%s,%s)",
                (authority["incarnation"], HASHES[0], HASHES[1], approved_id),
            )


def test_v5_semantic_root_is_framed_ordered_and_database_enforced(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        seeded = _generation(conn, spans=("span-a", "span-b"))
        _admit(conn, seeded, approval="approval-a", event="event-a", sequence=1, span="span-a")
        _admit(conn, seeded, approval="approval-b", event="event-b", sequence=2, span="span-b")
        conn.commit()

        conn.execute("SAVEPOINT coverage_order")
        for span, event in (("span-b", "event-b"), ("span-a", "event-a")):
            conn.execute(
                "SELECT public.brain_record_factual_assertion_coverage('owner-a','generation-a',%s,%s)",
                (span, event),
            )
        reverse_root = _semantic_root(conn)
        conn.execute("ROLLBACK TO SAVEPOINT coverage_order")
        for span, event in (("span-a", "event-a"), ("span-b", "event-b")):
            conn.execute(
                "SELECT public.brain_record_factual_assertion_coverage('owner-a','generation-a',%s,%s)",
                (span, event),
            )
        forward_root = _semantic_root(conn)
        assert forward_root == reverse_root
        conn.commit()
        with pytest.raises(psycopg.Error, match="semantic root"):
            conn.execute(
                "SELECT public.brain_publish_factual_snapshot("
                "'owner-a','snapshot-tampered','generation-a',%s,%s,%s,2,now(),NULL,%s)",
                (
                    "0" * 64,
                    _artifact(conn, "tampered-coverage"),
                    seeded["manifest"],
                    _artifact(conn, "tampered-snapshot"),
                ),
            )
        conn.rollback()

        assert conn.execute(
            "SELECT public.brain_publish_factual_snapshot("
            "'owner-a','snapshot-valid','generation-a',%s,%s,%s,2,now(),NULL,%s) AS id",
            (
                forward_root,
                _artifact(conn, "valid-semantic-coverage"),
                seeded["manifest"],
                _artifact(conn, "valid-semantic-snapshot"),
            ),
        ).fetchone()["id"] == "snapshot-valid"


def test_v5_semantic_root_framing_prevents_field_boundary_collisions(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        _generation(conn, spans=("span-a", "span-b"))
        review_hashes = {
            span: _artifact(conn, f"collision-{span}-review")
            for span in ("span-a", "span-b")
        }
        reviewed_at = datetime.now(UTC) - timedelta(seconds=1)
        conn.execute("SAVEPOINT collision_state")
        roots = []
        for reasons in (("a", "bc"), ("ab", "c")):
            for span, reason in zip(("span-a", "span-b"), reasons, strict=True):
                conn.execute(
                    "SELECT public.brain_review_factual_exclusion("
                    "'owner-a','generation-a',%s,%s,%s,'reviewer',%s)",
                    (span, reason, review_hashes[span], reviewed_at),
                )
            roots.append(_semantic_root(conn))
            conn.execute("ROLLBACK TO SAVEPOINT collision_state")
        assert roots[0] != roots[1]


def test_v5_semantic_root_binds_active_contradiction_transition_and_lineage(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        _generation(conn)
        conn.execute(
            "SELECT public.brain_create_factual_contradiction("
            "'owner-a','contradiction-root','generation-a',%s)",
            (_artifact(conn, "contradiction-root"),),
        )
        opened_id = conn.execute(
            "SELECT public.brain_append_factual_contradiction_event("
            "'owner-a','contradiction-root',1,'opened','active','noncritical',NULL,NULL) AS id"
        ).fetchone()["id"]
        confirmed_id = conn.execute(
            "SELECT public.brain_append_factual_contradiction_event("
            "'owner-a','contradiction-root',2,'confirmed','active','noncritical',%s,NULL) AS id",
            (opened_id,),
        ).fetchone()["id"]
        confirmed_root = _semantic_root(conn)

        conn.execute("SET session_replication_role=replica")
        conn.execute(
            "UPDATE public.brain_factual_contradiction_events SET event_type='opened' "
            "WHERE contradiction_event_id=%s",
            (confirmed_id,),
        )
        conn.execute("SET session_replication_role=origin")
        opened_root = _semantic_root(conn)

        conn.execute("SET session_replication_role=replica")
        conn.execute(
            "UPDATE public.brain_factual_contradiction_events "
            "SET event_type='confirmed',previous_event_id=NULL WHERE contradiction_event_id=%s",
            (confirmed_id,),
        )
        conn.execute("SET session_replication_role=origin")
        unlinked_root = _semantic_root(conn)

        assert len({confirmed_root, opened_root, unlinked_root}) == 3


def test_v5_exact_function_fingerprint_rejects_literal_whitespace_bypass(authority_v5_pg):
    with psycopg.connect(authority_v5_pg, row_factory=dict_row) as conn:
        definition = conn.execute(
            "SELECT pg_get_functiondef('public.brain_review_factual_exclusion("
            "text,text,text,text,text,text,timestamptz)'::regprocedure) AS definition"
        ).fetchone()["definition"]
        mutated = definition.replace(
            "'closed factual generation is required'",
            "'closed  factual generation is required'",
        )
        assert mutated != definition
        assert " ".join(mutated.split()) == " ".join(definition.split())
        conn.execute(mutated)
        conn.commit()
        with pytest.raises(RuntimeError, match="function body fingerprint|exact catalog contract"):
            schema.verify_brain_schema_v5(conn)
