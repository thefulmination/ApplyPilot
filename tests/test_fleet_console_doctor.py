"""Console-side Fleet Doctor tests: the action allow-list is EXACTLY the 7 existing +
the two doctor_* actions + the three challenge_* triage ops (no LinkedIn APPLY/scrape
action), and the two doctor_* actions are conservative/bookkeeping.
"""
from __future__ import annotations

import pytest

from applypilot.apply import pgqueue
from applypilot.fleet import console_app, doctor


_EXISTING_SEVEN = {
    "arm_canary", "lift_canary", "pause", "resume", "reclaim", "set_cap", "expand_searches",
}
_NEW_TWO = {"doctor_revert", "doctor_dismiss"}
# Task 3: challenge triage ops. They route ONLY through queue.resolve_challenge /
# resolve_linkedin_challenge (lane-routed); they never apply, scrape, or resume
# LinkedIn, so they don't violate the "no LinkedIn action" guard below.
_CHALLENGE_THREE = {"challenge_requeue", "challenge_skip", "challenge_skip_host"}


def test_actions_allowlist_is_exactly_seven_plus_two_and_no_linkedin():
    keys = set(console_app._ACTIONS)
    assert keys == _EXISTING_SEVEN | _NEW_TWO | _CHALLENGE_THREE, keys
    # No action key mentions linkedin (D2/D6).
    assert not any("linkedin" in k.lower() for k in keys)


def test_doctor_revert_clears_timeout_override_and_marks_reverted(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        # apply a timeout bump via the Doctor. PRODUCTION SHAPE: decide() stamps host=<triggering
        # host> while scope_key='ats' (the lane the single override governs). The earlier fixture
        # omitted host, which hid a bug where the knob_id revert never marked the diagnosis
        # 'reverted' for a host-carrying timeout_bump (COALESCE(host,lane,'')='ats' was FALSE).
        # H7: the ceiling dropped below the watchdog kill (now 540); use a valid in-range bump.
        action = {"knob_type": "timeout_bump", "actuator": "agent_timeout_override", "scope_key": "ats", "host": "slow.com", "lane": "ats",
                  "reason": "timeout", "new_timeout": 390, "current_default": 300,
                  "cluster_key": "timeout|-|-|ats", "diagnosis": "d", "recommendation": "r",
                  "sample_count": 3, "severity": "warn"}
        doctor.apply_auto(conn, action)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM fleet_knobs WHERE knob_type='timeout_bump' AND active")
            knob_id = cur.fetchone()["id"]
            cur.execute("SELECT agent_timeout_override FROM fleet_config WHERE id=1")
            assert cur.fetchone()["agent_timeout_override"] == 390
        # The UI always sends knob_id (console_app.py:1266); exercise exactly that path.
        msg = console_app._do_doctor_revert(conn, {"knob_id": knob_id})
        assert "Reverted" in msg
        with conn.cursor() as cur:
            cur.execute("SELECT active FROM fleet_knobs WHERE id=%s", (knob_id,))
            assert cur.fetchone()["active"] is False
            cur.execute("SELECT agent_timeout_override FROM fleet_config WHERE id=1")
            assert cur.fetchone()["agent_timeout_override"] is None  # back to default
            # The audit diagnosis must be flipped to 'reverted' (D3 audit trail intact), even
            # though it carries a host and the knob's scope is the lane.
            cur.execute("SELECT status FROM fleet_diagnoses WHERE auto_action='timeout_bump'")
            assert cur.fetchone()["status"] == "reverted"


def test_doctor_revert_pace_restores_host_gap_and_marks_reverted(fleet_db):
    """Reversing a host pace knob (knob_id path, production shape: scope='host:'||<host>,
    diagnosis host=<host>) must restore the host's min_gap to its base AND flip the diagnosis
    to 'reverted' (the old host/lane COALESCE match left it stuck in 'auto_applied')."""
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO rate_governor (scope_key, min_gap_seconds, base_min_gap_seconds) "
                        "VALUES ('host:slow.com', 90, 90)")
        conn.commit()
        action = {"knob_type": "pace_or_pause", "actuator": "doctor_min_gap_floor", "op_kind": "pace", "scope_key": "host:slow.com",
                  "host": "slow.com", "lane": "ats", "reason": "hard_block",
                  "cluster_key": "pace_or_pause|slow.com|pace|ats", "diagnosis": "d",
                  "recommendation": "r", "sample_count": 3, "severity": "warn",
                  "old_gap": 90, "new_gap": 270}
        doctor.apply_auto(conn, action)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM fleet_knobs WHERE knob_type='pace_or_pause' AND active")
            knob_id = cur.fetchone()["id"]
            # H4: the pace lives in the Doctor-owned floor; min_gap_seconds (breaker column) is untouched.
            cur.execute("SELECT min_gap_seconds, doctor_min_gap_floor FROM rate_governor WHERE scope_key='host:slow.com'")
            row = cur.fetchone()
            assert row["doctor_min_gap_floor"] == 270  # floor raised
            assert row["min_gap_seconds"] == 90        # breaker column untouched
        console_app._do_doctor_revert(conn, {"knob_id": knob_id})
        with conn.cursor() as cur:
            cur.execute("SELECT active FROM fleet_knobs WHERE id=%s", (knob_id,))
            assert cur.fetchone()["active"] is False
            cur.execute("SELECT min_gap_seconds, doctor_min_gap_floor FROM rate_governor WHERE scope_key='host:slow.com'")
            row = cur.fetchone()
            assert row["doctor_min_gap_floor"] is None  # H4: floor cleared by Reverse
            assert row["min_gap_seconds"] == 90         # breaker column still untouched
            cur.execute("SELECT status FROM fleet_diagnoses WHERE auto_action='pace_or_pause:pace'")
            assert cur.fetchone()["status"] == "reverted"


def test_doctor_revert_clears_its_own_ats_pause_never_shared_paused(fleet_db):
    """H1/H8: a Doctor ATS pause writes ats_paused (NOT the shared fleet_config.paused). Reverse on
    a Doctor-authored ATS pause DOES clear ats_paused (the v1 Reverse was a no-op so it looked
    broken) and never touches the shared flag."""
    with pgqueue.connect(fleet_db) as conn:
        action = {"knob_type": "pace_or_pause", "op_kind": "pause", "actuator": "ats_paused",
                  "scope_key": "ats", "lane": "ats", "reason": "hard_block",
                  "cluster_key": "pp|lane|pause|ats", "diagnosis": "d", "recommendation": "r",
                  "sample_count": 5, "severity": "severe"}
        doctor.apply_auto(conn, action)
        with conn.cursor() as cur:
            cur.execute("SELECT paused, ats_paused, ats_pause_source FROM fleet_config WHERE id=1")
            c = cur.fetchone()
            assert c["paused"] is False and c["ats_paused"] is True and c["ats_pause_source"] == "doctor"
            cur.execute("SELECT id FROM fleet_knobs WHERE knob_type='pace_or_pause' AND active")
            knob_id = cur.fetchone()["id"]
        console_app._do_doctor_revert(conn, {"knob_id": knob_id})
        with conn.cursor() as cur:
            cur.execute("SELECT active FROM fleet_knobs WHERE id=%s", (knob_id,))
            assert cur.fetchone()["active"] is False
            cur.execute("SELECT paused, ats_paused FROM fleet_config WHERE id=1")
            c = cur.fetchone()
            assert c["ats_paused"] is False  # H8: Doctor's OWN pause cleared by Reverse
            assert c["paused"] is False      # shared kill switch never touched


def test_doctor_revert_never_clears_an_operator_ats_pause(fleet_db):
    """H8: if an operator/cost set ats_paused (source != 'doctor'), a Doctor knob Reverse must
    NOT clear it."""
    with pgqueue.connect(fleet_db) as conn:
        # Simulate a Doctor pause knob whose ats_paused was later taken over by a non-doctor source.
        action = {"knob_type": "pace_or_pause", "op_kind": "pause", "actuator": "ats_paused",
                  "scope_key": "ats", "lane": "ats", "reason": "hard_block",
                  "cluster_key": "pp|lane|pause|ats", "diagnosis": "d", "recommendation": "r",
                  "sample_count": 5, "severity": "severe"}
        doctor.apply_auto(conn, action)
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET ats_pause_source='operator' WHERE id=1")
            conn.commit()
            cur.execute("SELECT id FROM fleet_knobs WHERE knob_type='pace_or_pause' AND active")
            knob_id = cur.fetchone()["id"]
        console_app._do_doctor_revert(conn, {"knob_id": knob_id})
        with conn.cursor() as cur:
            cur.execute("SELECT ats_paused FROM fleet_config WHERE id=1")
            assert cur.fetchone()["ats_paused"] is True  # operator pause preserved


def test_doctor_dismiss_marks_recommendation_dismissed(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fleet_diagnoses (reason, host, lane, diagnosis, recommendation, status) "
                "VALUES ('agent','x.com','ats','d','r','recommended') RETURNING id")
            did = cur.fetchone()["id"]
        conn.commit()
        msg = console_app._do_doctor_dismiss(conn, {"diagnosis_id": did})
        assert "dismissed" in msg.lower()
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM fleet_diagnoses WHERE id=%s", (did,))
            assert cur.fetchone()["status"] == "dismissed"


def test_doctor_dismiss_rejects_bad_id(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with pytest.raises(ValueError):
            console_app._do_doctor_dismiss(conn, {"diagnosis_id": "not-int"})
        with pytest.raises(ValueError):
            console_app._do_doctor_dismiss(conn, {"diagnosis_id": 999999})  # no such row


def test_diagnostics_read_shapes(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        # one active knob + one recommendation
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fleet_knobs (knob_type, scope_key, value_text, reason, active, expires_at) "
                "VALUES ('host_skip','acme.com','skip','hard_block', TRUE, now()+interval '1 day')")
            cur.execute(
                "INSERT INTO fleet_diagnoses (reason, host, lane, diagnosis, recommendation, status) "
                "VALUES ('agent','x.com','ats','dd','rr','recommended')")
        conn.commit()
        d = console_app._diagnostics(conn)
    assert "clusters" in d and "auto_fixes" in d and "recommendations" in d
    assert any(a["knob_type"] == "host_skip" for a in d["auto_fixes"])
    assert any(r["reason"] == "agent" for r in d["recommendations"])
