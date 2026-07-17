from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue  # noqa: E402
from applypilot.fleet import console_app, heartbeat  # noqa: E402


def _ensure_lane_pin_columns(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE fleet_config "
            "ADD COLUMN IF NOT EXISTS ats_canary_worker_id TEXT, "
            "ADD COLUMN IF NOT EXISTS ats_canary_version TEXT, "
            "ADD COLUMN IF NOT EXISTS linkedin_canary_worker_id TEXT, "
            "ADD COLUMN IF NOT EXISTS linkedin_canary_version TEXT"
        )
    conn.commit()


def test_build_status_exposes_worker_version_drift(fleet_db, monkeypatch) -> None:
    pinned = "0.3.0+git.main.aaaaaaa"
    stale = "0.3.0+git.main.bbbbbbb"
    generic_canary = "0.3.0+git.main.ccccccc"
    ats_canary = "0.3.0+git.main.ddddddd"
    linkedin_canary = "0.3.0+git.main.eeeeeee"
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)

    with pgqueue.connect(fleet_db) as conn:
        _ensure_lane_pin_columns(conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_config SET pinned_worker_version=%s, canary_version=%s, "
                "canary_worker_id=%s, ats_canary_version=%s, ats_canary_worker_id=%s, "
                "linkedin_canary_version=%s, linkedin_canary_worker_id=%s WHERE id=1",
                (
                    pinned,
                    generic_canary,
                    "compute-canary",
                    ats_canary,
                    "ats-canary",
                    linkedin_canary,
                    "linkedin-canary",
                ),
            )
        conn.commit()
        heartbeat.beat(
            conn,
            "ats-canary",
            machine_owner="m2",
            home_ip="100.1.1.1",
            role="apply",
            state="idle",
            sw_version=ats_canary,
        )
        heartbeat.beat(
            conn,
            "linkedin-canary",
            machine_owner="m4",
            home_ip="100.1.1.2",
            role="linkedin",
            state="idle",
            sw_version=stale,
        )
        heartbeat.beat(
            conn,
            "compute-canary",
            machine_owner="home",
            home_ip="100.1.1.3",
            role="compute",
            state="idle",
            sw_version=generic_canary,
        )
        heartbeat.beat(
            conn,
            "discovery-stale",
            machine_owner="home",
            home_ip="100.1.1.4",
            role="discovery",
            state="idle",
            sw_version=stale,
        )
        heartbeat.beat(
            conn,
            "watchdog",
            machine_owner="home",
            home_ip="100.1.1.5",
            role="watchdog",
            state="idle",
            sw_version=None,
        )

    status = console_app.build_status()

    assert status["versions"]["pinned_worker_version"] == pinned
    assert status["versions"]["canary_version"] == generic_canary
    assert status["versions"]["ats_canary_version"] == ats_canary
    assert status["versions"]["ats_canary_worker_id"] == "ats-canary"
    assert status["versions"]["linkedin_canary_version"] == linkedin_canary
    assert status["versions"]["linkedin_canary_worker_id"] == "linkedin-canary"
    assert status["versions"]["worker_versions"] == {
        ats_canary: 1,
        generic_canary: 1,
        stale: 2,
    }
    assert status["versions"]["drifted_workers"] == [
        {"worker_id": "discovery-stale", "machine_owner": "home", "sw_version": stale},
        {"worker_id": "linkedin-canary", "machine_owner": "m4", "sw_version": stale},
    ]
    workers = {row["worker_id"]: row for row in status["workers"]}
    assert workers["ats-canary"]["sw_version"] == ats_canary
