from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import console_app


def test_deployment_info_reports_console_schema_and_worker_versions(fleet_db, monkeypatch):
    monkeypatch.setattr(
        console_app,
        "_git_text",
        lambda args: {
            ("rev-parse", "--abbrev-ref", "HEAD"): "codex/fleet-operations-console",
            ("rev-parse", "--short", "HEAD"): "abc1234",
        }.get(tuple(args)),
    )

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat "
                "(worker_id, machine_owner, role, state, sw_version, last_beat) "
                "VALUES "
                "('m4-0','m4','apply','idle','worker-new',now()), "
                "('m2-0','m2','apply','idle',NULL,now())"
            )
        conn.commit()

        result = console_app._deployment_info(conn)

    assert result["console"]["branch"] == "codex/fleet-operations-console"
    assert result["console"]["commit"] == "abc1234"
    assert result["schema"]["agent_telemetry"] is True
    assert result["schema"]["audit_table"] is True
    assert result["worker_versions"] == [
        {"version": "(unknown)", "workers": 1},
        {"version": "worker-new", "workers": 1},
    ]


def test_status_workers_include_machine_role_state_and_version_health(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat "
                "(worker_id, machine_owner, home_ip, role, state, current_job, sw_version, last_beat) "
                "VALUES "
                "('m4-0','m4','100.69.68.103','apply','idle',NULL,'abc1234',now()), "
                "('m2-0','m2','100.77.65.8','apply','idle',NULL,'oldsha',now() - interval '10 minutes')"
            )
        conn.commit()

        result = console_app._workers(conn)

    by_id = {row["worker_id"]: row for row in result}
    assert by_id["m4-0"]["machine_owner"] == "m4"
    assert by_id["m4-0"]["machine_display_name"] == "GGGTower"
    assert by_id["m4-0"]["role"] == "apply"
    assert by_id["m4-0"]["state"] == "idle"
    assert by_id["m4-0"]["sw_version"] == "abc1234"
    assert by_id["m4-0"]["health"] == "alive"
    assert by_id["m2-0"]["health"] == "stale"
    assert by_id["m2-0"]["machine_display_name"] == "TARPON"
