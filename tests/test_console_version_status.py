from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import console_app, heartbeat


def test_build_status_exposes_worker_version_drift(fleet_db, monkeypatch) -> None:
    pinned = "0.3.0+git.main.aaaaaaa"
    stale = "0.3.0+git.main.bbbbbbb"
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_config SET pinned_worker_version=%s, canary_version=%s, "
                "canary_worker_id=%s WHERE id=1",
                (pinned, "0.3.0+git.main.ccccccc", "m4-0"),
            )
        conn.commit()
        heartbeat.beat(
            conn,
            "m2-0",
            machine_owner="m2",
            home_ip="100.1.1.1",
            role="apply",
            state="idle",
            sw_version=pinned,
        )
        heartbeat.beat(
            conn,
            "m4-0",
            machine_owner="m4",
            home_ip="100.1.1.2",
            role="apply",
            state="idle",
            sw_version=stale,
        )

    status = console_app.build_status()

    assert status["versions"]["pinned_worker_version"] == pinned
    assert status["versions"]["canary_version"] == "0.3.0+git.main.ccccccc"
    assert status["versions"]["worker_versions"] == {pinned: 1, stale: 1}
    assert status["versions"]["drifted_workers"] == [
        {"worker_id": "m4-0", "machine_owner": "m4", "sw_version": stale}
    ]
    workers = {row["worker_id"]: row for row in status["workers"]}
    assert workers["m2-0"]["sw_version"] == pinned
    assert workers["m4-0"]["sw_version"] == stale
