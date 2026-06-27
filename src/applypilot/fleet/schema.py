"""Apply the v3 fleet schema (base + extensions) idempotently."""
from __future__ import annotations

from pathlib import Path

from applypilot.apply import pgqueue

_SCHEMA_V3_SQL = Path(__file__).with_name("schema_v3.sql")


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
