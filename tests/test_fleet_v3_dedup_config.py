"""Dedup key normalization (pure) + fleet_config v3 helpers (PG)."""
from __future__ import annotations

from applypilot.apply import pgqueue
from applypilot.fleet import config as fcfg
from applypilot.fleet import dedup


# ---- dedup (pure, no Postgres) -------------------------------------------------

def test_dedup_collapses_across_boards():
    # same company+role on different boards -> same key
    k1 = dedup.dedup_key("Acme, Inc.", "Chief of Staff")
    k2 = dedup.dedup_key("Acme Inc", "Chief of Staff (Remote)")
    k3 = dedup.dedup_key("Acme", "CoS")
    assert k1 == k2 == k3


def test_dedup_collapses_seniority_and_level():
    assert dedup.dedup_key("Foo", "Senior Engineer II") == dedup.dedup_key("Foo", "Engineer")
    assert dedup.dedup_key("Foo", "Staff Engineer") == dedup.dedup_key("Foo", "Engineer")


def test_dedup_distinguishes_real_differences():
    assert dedup.dedup_key("Acme", "Chief of Staff") != dedup.dedup_key("Acme", "Data Scientist")
    assert dedup.dedup_key("Acme", "Chief of Staff") != dedup.dedup_key("Beta", "Chief of Staff")


def test_dedup_stable_and_short():
    k = dedup.dedup_key("Acme", "Chief of Staff")
    assert isinstance(k, str) and len(k) == 20
    assert k == dedup.dedup_key("Acme", "Chief of Staff")  # deterministic


def test_normalize_role_strips_reqid_and_parens():
    assert dedup.normalize_role("Business Operations (NYC) Req #12345") == "business operations"


# ---- fleet_config v3 (Postgres) ------------------------------------------------

def test_approval_policy_roundtrip(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        fcfg.set_approval_policy(conn, min_fit=8.0, min_confidence=0.7,
                                 exclude_flags=["stretch", "pivot_penalty"], sampling_rate=0.1)
        pol = fcfg.get_approval_policy(conn)
        cfg = fcfg.get_config(conn)
    assert pol["min_fit"] == 8.0 and pol["min_confidence"] == 0.7
    assert "stretch" in pol["exclude_flags"]
    assert abs(float(cfg["approval_sampling_rate"]) - 0.1) < 1e-6


def test_cost_caps_and_pause(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        fcfg.set_cost_caps(conn, daily_usd=5.0, total_usd=50.0)
        fcfg.set_paused(conn, True)
        cfg = fcfg.get_config(conn)
    assert float(cfg["cost_cap_daily_usd"]) == 5.0
    assert float(cfg["cost_cap_total_usd"]) == 50.0
    assert cfg["paused"] is True


def test_version_for_worker_canary(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        fcfg.set_pinned_version(conn, "1.2.0", canary_version="1.3.0-rc1", canary_worker_id="w-canary")
        assert fcfg.version_for_worker(conn, "w-canary") == "1.3.0-rc1"
        assert fcfg.version_for_worker(conn, "w-other") == "1.2.0"
