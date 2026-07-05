from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import console_diagnosis


def _seed_apply_job(
    conn,
    *,
    url: str,
    company: str = "Acme",
    title: str = "Engineer",
    status: str = "queued",
    approved_batch: str | None = "batch-1",
    dedup_key: str | None = None,
    score: float = 8.0,
    target_host: str = "boards.greenhouse.io",
    lane: str = "ats",
    est_cost_usd: float = 0.0,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, company, title, application_url, score, lane, status, approved_batch, "
            "dedup_key, target_host, apply_domain, est_cost_usd, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())",
            (
                url,
                company,
                title,
                url + "/apply",
                score,
                lane,
                status,
                approved_batch,
                dedup_key or f"{company.lower()}::{title.lower()}",
                target_host,
                target_host,
                est_cost_usd,
            ),
        )


def test_ats_queue_diagnosis_counts_dedup_blocked_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply_job(
            conn,
            url="https://boards.greenhouse.io/acme/jobs/1",
            dedup_key="acme::engineer",
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applied_set (dedup_key, company, applied_url) "
                "VALUES ('acme::engineer', 'Acme', 'https://already/applied')"
            )
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    ats = result["ats"]
    assert ats["queued"] == 1
    assert ats["approved"] == 1
    assert ats["dedup_blocked"] == 1
    assert ats["leaseable"] == 0
    assert result["state"]["code"] == "idle_no_leasable_jobs"
    assert "dedup" in result["state"]["reason"].lower()


def test_ats_queue_diagnosis_counts_leaseable_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply_job(
            conn,
            url="https://boards.greenhouse.io/acme/jobs/2",
            dedup_key="acme::analyst",
        )
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    ats = result["ats"]
    assert ats["queued"] == 1
    assert ats["approved"] == 1
    assert ats["dedup_blocked"] == 0
    assert ats["leaseable"] == 1
    assert result["state"]["code"] == "ready_to_apply"


def test_ats_queue_diagnosis_spend_cap_reached_is_not_leaseable(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply_job(
            conn,
            url="https://boards.greenhouse.io/acme/jobs/3",
            dedup_key="acme::designer",
            est_cost_usd=1.0,
        )
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET spend_cap_usd=1.0 WHERE id=1")
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    ats = result["ats"]
    assert ats["approved"] == 1
    assert ats["dedup_blocked"] == 0
    assert ats["leaseable"] == 0
    assert result["state"]["code"] != "ready_to_apply"


def test_ats_queue_diagnosis_global_cap_reached_is_not_leaseable(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply_job(
            conn,
            url="https://boards.greenhouse.io/acme/jobs/5",
            dedup_key="acme::qa",
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rate_governor (scope_key, count_24h, daily_cap) "
                "VALUES ('global', 3, 3)"
            )
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    assert result["ats"]["leaseable"] == 0
    assert result["state"]["code"] != "ready_to_apply"


def test_ats_queue_diagnosis_host_doctor_skip_is_not_leaseable(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply_job(
            conn,
            url="https://boards.greenhouse.io/acme/jobs/6",
            dedup_key="acme::support",
            target_host="boards.greenhouse.io",
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rate_governor (scope_key, doctor_skip_until) "
                "VALUES ('host:boards.greenhouse.io', now() + interval '1 hour')"
            )
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    assert result["ats"]["leaseable"] == 0
    assert result["state"]["code"] != "ready_to_apply"


def test_ats_queue_diagnosis_non_ats_lane_not_counted_in_depth(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply_job(
            conn,
            url="https://boards.greenhouse.io/acme/jobs/4",
            dedup_key="acme::ops",
            lane="compute",
        )
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    ats = result["ats"]
    assert ats["queued"] == 0
    assert ats["approved"] == 0
    assert ats["dedup_blocked"] == 0
    assert ats["leaseable"] == 0
    assert result["state"]["code"] == "idle_no_leasable_jobs"


def test_linkedin_canary_exhaustion_is_lane_specific(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO linkedin_queue "
                "(url, company, title, application_url, score, lane, status, approved_batch, dedup_key, updated_at) "
                "VALUES ('https://www.linkedin.com/jobs/view/1','Beta','Analyst',"
                "'https://www.linkedin.com/jobs/view/1',8,'ats','queued','batch-li','beta::analyst',now())"
            )
            cur.execute(
                "UPDATE fleet_config SET linkedin_canary_enabled=TRUE, "
                "linkedin_canary_remaining=0, canary_enabled=FALSE, canary_remaining=NULL WHERE id=1"
            )
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    assert result["linkedin"]["queued"] == 1
    assert result["linkedin"]["approved"] == 1
    assert result["linkedin"]["leaseable"] == 0
    assert result["linkedin"]["canary_exhausted"] is True
    assert result["ats"]["canary_exhausted"] is False


def test_linkedin_owner_ip_context_missing_prevents_leaseable_overreport(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO linkedin_queue "
                "(url, company, title, application_url, score, lane, status, approved_batch, dedup_key, updated_at) "
                "VALUES ('https://www.linkedin.com/jobs/view/2','Beta','Manager',"
                "'https://www.linkedin.com/jobs/view/2',8,'ats','queued','batch-li','beta::manager',now())"
            )
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    assert result["linkedin"]["approved"] == 1
    assert result["linkedin"]["leaseable"] == 0
    assert result["linkedin"]["owner_ip_context_known"] is False


def test_linkedin_owner_ip_mismatch_prevents_leaseable_overreport(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO linkedin_queue "
                "(url, company, title, application_url, score, lane, status, approved_batch, dedup_key, updated_at) "
                "VALUES ('https://www.linkedin.com/jobs/view/3','Beta','Director',"
                "'https://www.linkedin.com/jobs/view/3',8,'ats','queued','batch-li','beta::director',now())"
            )
            cur.execute(
                "INSERT INTO workers (worker_id, public_ip, capabilities) "
                "VALUES ('li-worker', '203.0.113.10', '{\"can_linkedin\": true}'::jsonb)"
            )
            cur.execute(
                "INSERT INTO worker_heartbeat (worker_id, home_ip, role, state, last_beat) "
                "VALUES ('li-worker', '203.0.113.11', 'linkedin', 'idle', now())"
            )
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    assert result["linkedin"]["leaseable"] == 0
    assert result["linkedin"]["owner_ip_context_known"] is True
    assert result["linkedin"]["owner_ip_ready"] is False


def test_queue_diagnosis_rolls_back_when_query_raises():
    class RaisingCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    class Conn:
        def __init__(self):
            self.rolled_back = False

        def cursor(self):
            return RaisingCursor()

        def rollback(self):
            self.rolled_back = True

    conn = Conn()
    with pytest.raises(RuntimeError, match="boom"):
        console_diagnosis.queue_diagnosis(conn)

    assert conn.rolled_back is True


@pytest.mark.parametrize("failure", ["execute", "fetchall"])
def test_browser_health_rolls_back_when_query_raises(failure):
    class RaisingCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *_args, **_kwargs):
            if failure == "execute":
                raise RuntimeError("execute boom")

        def fetchall(self):
            if failure == "fetchall":
                raise RuntimeError("fetchall boom")
            return []

    class Conn:
        def __init__(self):
            self.rolled_back = False

        def cursor(self):
            return RaisingCursor()

        def rollback(self):
            self.rolled_back = True

    conn = Conn()
    with pytest.raises(RuntimeError, match=f"{failure} boom"):
        console_diagnosis.browser_health(conn)

    assert conn.rolled_back is True


def test_browser_health_counts_apply_workers_and_skips_non_apply_and_unknown(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat "
                "(worker_id, machine_owner, role, state, last_error, recent_log, last_beat) "
                "VALUES "
                "('apply-known', 'm4', 'apply', 'idle', 'RESULT:FAILED:login_issue', '', now()), "
                "('apply-unknown', 'm4', 'apply', 'idle', '', 'ordinary heartbeat', now()), "
                "('linkedin-known', 'home', 'linkedin', 'idle', 'RESULT:FAILED:login_issue', '', now())"
            )
        conn.commit()

        result = console_diagnosis.browser_health(conn)

    assert result["counts"] == {"login_gate": 1}
    assert result["examples"] == {
        "login_gate": {
            "worker_id": "apply-known",
            "machine_owner": "m4",
            "severity": "warn",
        }
    }


def test_recommendation_for_dedup_blocked_queue(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply_job(
            conn,
            url="https://boards.greenhouse.io/acme/jobs/3",
            dedup_key="acme::pm",
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applied_set (dedup_key, company, applied_url) "
                "VALUES ('acme::pm', 'Acme', 'https://already/applied')"
            )
        conn.commit()

        result = console_diagnosis.full_diagnosis(conn)

    rec = result["recommendations"][0]
    assert rec["code"] == "reconcile_dedup_blocked_queue"
    assert rec["action_type"] == "manual_runbook"
    assert "remediator" in rec["command"].lower() or "reconcile" in rec["command"].lower()


def test_recommendation_for_linkedin_canary_exhausted(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO linkedin_queue "
                "(url, company, title, application_url, score, lane, status, approved_batch, dedup_key) "
                "VALUES ('https://www.linkedin.com/jobs/view/4','Acme','Lead',"
                "'https://www.linkedin.com/jobs/view/4',8,'ats','queued','li-batch','acme::lead')"
            )
            cur.execute(
                "UPDATE fleet_config SET linkedin_canary_enabled=TRUE, "
                "linkedin_canary_remaining=0 WHERE id=1"
            )
        conn.commit()

        result = console_diagnosis.full_diagnosis(conn)

    codes = {r["code"] for r in result["recommendations"]}
    assert "rearm_linkedin_canary" in codes


def test_recommendation_for_browser_server_unavailable():
    queue = {
        "ats": {"approved": 0, "leaseable": 0, "dedup_blocked": 0},
        "linkedin": {"queued": 0, "canary_exhausted": False},
    }
    browser = {"counts": {"browser_server_unavailable": 1}}

    result = console_diagnosis.recommendations_from(queue, browser)

    assert [r["code"] for r in result] == ["restart_browser_backend"]
