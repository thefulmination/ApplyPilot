"""Apply the v3 fleet schema (base + extensions) idempotently."""
from __future__ import annotations

from pathlib import Path

from applypilot.apply import pgqueue

_SCHEMA_V3_SQL = Path(__file__).with_name("schema_v3.sql")

_APPLY_RESULT_EVENT_REQUIRED_COLUMNS = frozenset({
    "queue_name",
    "url",
    "worker_id",
    "status",
    "apply_status",
    "apply_error",
    "target_host",
    "home_ip",
    "agent",
    "agent_model",
    "est_cost_usd",
    "apply_duration_ms",
    "result_line",
    "source",
    "route",
    "failure_class",
    "tool_calls_total",
    "application_tool_calls",
    "last_tool",
    "host_policy",
    "result_metadata",
})


def ensure_schema_v3(conn) -> None:
    """Idempotently apply the base fleet schema then the v3 extensions.

    Safe to run on every broker/home/worker startup. Runs ``pgqueue.ensure_schema``
    (apply_queue / fleet_config / fleet_assets) first, then layers the v3 tables +
    columns on top. Commits.
    """
    pgqueue.ensure_schema(conn)
    sql = _SCHEMA_V3_SQL.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def require_apply_result_event_schema(conn) -> None:
    """Read-only worker compatibility check for result metadata columns.

    Remote workers commonly use the least-privilege ``fleet_worker`` role, which has
    DML grants but intentionally no DDL. The owner/home process must run
    ``ensure_schema_v3``; workers only verify that the columns they write exist before
    leasing a job.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'apply_result_events'"
        )
        cols = {
            row["column_name"] if hasattr(row, "get") else row[0]
            for row in cur.fetchall()
        }
    try:
        conn.rollback()
    except Exception:
        pass
    missing = sorted(_APPLY_RESULT_EVENT_REQUIRED_COLUMNS - cols)
    if missing:
        raise RuntimeError(
            "fleet schema is missing apply_result_events columns: "
            + ", ".join(missing)
            + "; run applypilot-fleet-apply-home with the owner/home DSN once to migrate "
            "before starting remote apply workers"
        )
