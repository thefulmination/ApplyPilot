from __future__ import annotations

from pathlib import Path

from applypilot.brain import schema


ROOT = Path(__file__).resolve().parents[1]
V6_SQL = ROOT / "src" / "applypilot" / "brain" / "schema_v6.sql"


def test_v6_is_checksum_pinned_and_registered() -> None:
    assert schema._SCHEMA_V6_SQL == V6_SQL
    assert schema._EXPECTED_V6_CHECKSUM == schema._schema_v6_checksum()
    assert schema._MIGRATION_V6_NAME == "brain schema v6 immutable artifact authority"
    assert schema.ensure_schema_v6 is schema.ensure_brain_schema_v6
    assert schema.verify_schema_v6 is schema.verify_brain_schema_v6


def test_v6_sql_orders_parent_before_children_and_preserves_function_owner() -> None:
    sql = V6_SQL.read_text(encoding="utf-8")
    request_insert = sql.index("INSERT INTO public.brain_artifact_authority_requests")
    registration_insert = sql.index("INSERT INTO public.brain_artifact_authority_registrations")
    assert request_insert < registration_insert
    lifecycle = sql.index("CREATE OR REPLACE FUNCTION public.brain_check_policy_lifecycle")
    assert sql.rfind("SET ROLE brain_schema_migrator", 0, lifecycle) > sql.rfind(
        "SET ROLE brain_artifact_authority_owner", 0, lifecycle
    )


def test_v6_sql_grants_only_the_required_authority_surface() -> None:
    sql = V6_SQL.read_text(encoding="utf-8")
    assert "GRANT SELECT, INSERT ON TABLE public.brain_artifacts" in sql
    assert "GRANT SELECT, INSERT ON TABLE public.brain_artifact_locations" in sql
    assert "GRANT USAGE, SELECT ON SEQUENCE public.brain_artifact_locations_artifact_location_id_seq" in sql
    assert "ALTER FUNCTION public.brain_register_authoritative_artifact_manifest" in sql
    assert "OWNER TO brain_artifact_authority_owner" in sql
    assert "REVOKE ALL ON FUNCTION public.brain_register_authoritative_artifact_manifest" in sql
    assert "GRANT EXECUTE ON FUNCTION public.brain_register_authoritative_artifact_manifest" in sql
    assert "FROM public.brain_artifact_authority_registrations registration" in sql
    assert "CREATE OR REPLACE FUNCTION public.brain_artifact_is_authoritative" in sql
