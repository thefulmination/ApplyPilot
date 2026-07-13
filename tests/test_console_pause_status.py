from datetime import datetime, timedelta, timezone

from applypilot.apply import pgqueue
from applypilot.fleet import console_app, queue


def test_gate_leasable_is_zero_when_ats_paused(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        policy = "console-pause-ats-policy"
        now = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fleet_decision_policies (policy_version,lane,status) "
                "VALUES (%s,'ats','active')",
                (policy,),
            )
            cur.execute(
                "UPDATE fleet_config SET ats_policy_version=%s WHERE id=1",
                (policy,),
            )
        queue.push_apply_jobs(conn, [{
            "url": "https://boards.greenhouse.io/acme/jobs/1",
            "company": "Acme",
            "title": "Chief of Staff",
            "application_url": "https://boards.greenhouse.io/acme/jobs/1/apply",
            "score": 9,
            "target_host": "boards.greenhouse.io",
            "decision_id": "decision-console-pause-1",
            "policy_version": policy,
            "decision_action": "apply",
            "qualification_verdict": "qualified",
            "qualification_score": 9.0,
            "qualification_floor": 7.0,
            "preference_score": 8.0,
            "outcome_score": 8.0,
            "final_score": 9.0,
            "decision_confidence": 0.9,
            "decision_created_at": now,
            "decision_expires_at": now + timedelta(days=1),
            "input_hash": "hash-console-pause-1",
        }], approved_batch="batch-1")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_config SET paused=FALSE, ats_paused=TRUE, "
                "ats_pause_source='operator', ats_apply_mode='canary', "
                "canary_enabled=TRUE, canary_remaining=5 WHERE id=1"
            )
        conn.commit()

        gate, _, _ = console_app._gate_and_queue(conn)

    assert gate["base_leasable"] == 1
    assert gate["leasable"] == 0
    assert gate["lease_gate_open"] is False
    assert gate["ats_paused"] is True
    assert gate["ats_pause_source"] == "operator"
    assert "ats_paused" in gate["halt_reasons"]


def test_console_arm_canary_refuses_ats_paused(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET paused=TRUE, ats_paused=TRUE, "
            "ats_pause_source='operator', canary_enabled=FALSE, canary_remaining=NULL WHERE id=1"
        )
        conn.commit()

    ok, msg = console_app.run_action({"action": "arm_canary", "k": 5})

    assert ok is False
    assert "ATS pause" in msg
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT paused, ats_paused, canary_enabled, canary_remaining FROM fleet_config WHERE id=1")
        cfg = cur.fetchone()
    assert cfg["paused"] is True
    assert cfg["ats_paused"] is True
    assert cfg["canary_enabled"] is False
    assert cfg["canary_remaining"] == 0


def test_console_lift_canary_disarms_without_unpausing(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET paused=TRUE, ats_paused=FALSE, "
            "ats_apply_mode='canary', canary_enabled=TRUE, canary_remaining=5 WHERE id=1"
        )
        conn.commit()

    ok, msg = console_app.run_action({"action": "lift_canary"})

    assert ok is True
    assert "disarmed" in msg.lower()
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT paused, ats_apply_mode, canary_enabled, canary_remaining FROM fleet_config WHERE id=1")
        cfg = cur.fetchone()
    assert cfg["paused"] is True
    assert cfg["ats_apply_mode"] == "stopped"
    assert cfg["canary_enabled"] is False
    assert cfg["canary_remaining"] is None
