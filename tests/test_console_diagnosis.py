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


def test_queue_diagnosis_surfaces_linkedin_block_when_ats_empty(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO linkedin_queue "
                "(url, company, title, application_url, score, lane, status, approved_batch, dedup_key, updated_at) "
                "VALUES ('https://www.linkedin.com/jobs/view/blocked','Beta','Analyst',"
                "'https://www.linkedin.com/jobs/view/blocked',8,'ats','queued','batch-li','beta::blocked',now())"
            )
            cur.execute(
                "UPDATE fleet_config SET linkedin_canary_enabled=TRUE, "
                "linkedin_canary_remaining=0, canary_enabled=FALSE, canary_remaining=NULL WHERE id=1"
            )
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    assert result["ats"]["queued"] == 0
    assert result["state"]["code"] == "linkedin_canary_exhausted"
    assert "LinkedIn" in result["state"]["reason"]


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


def test_operational_rollups_rolls_back_when_query_raises():
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
        console_diagnosis.operational_rollups(conn)

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
            "machine_display_name": "GGGTower",
            "severity": "warn",
            "logs_url": "/api/logs?worker=apply-known",
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


def test_recommendation_for_paused_fleet():
    queue = {
        "state": {"code": "paused"},
        "ats": {
            "approved": 12,
            "leaseable": 0,
            "dedup_blocked": 0,
            "paused": True,
            "ats_paused": True,
        },
        "linkedin": {"queued": 0, "canary_exhausted": False},
    }
    browser = {"counts": {}}

    result = console_diagnosis.recommendations_from(queue, browser)

    assert result[0]["code"] == "review_fleet_pause"
    assert result[0]["severity"] == "halted"
    assert "paused" in result[0]["reason"].lower()


def test_recommendation_for_ats_lane_pause():
    queue = {
        "state": {"code": "ats_paused"},
        "ats": {
            "approved": 12,
            "leaseable": 0,
            "dedup_blocked": 0,
            "paused": False,
            "ats_paused": True,
        },
        "linkedin": {"queued": 0, "canary_exhausted": False},
    }
    browser = {"counts": {}}

    result = console_diagnosis.recommendations_from(queue, browser)

    assert result[0]["code"] == "review_ats_pause"
    assert result[0]["severity"] == "halted"
    assert "ATS lane" in result[0]["title"]


def test_recommendation_for_login_or_captcha_browser_walls():
    queue = {
        "state": {"code": "ready_to_apply"},
        "ats": {"approved": 12, "leaseable": 12, "dedup_blocked": 0},
        "linkedin": {"queued": 0, "canary_exhausted": False},
    }
    browser = {"counts": {"login_gate": 2, "captcha": 3}}

    result = console_diagnosis.recommendations_from(queue, browser)

    assert result[0]["code"] == "review_browser_walls"
    assert result[0]["severity"] == "warn"
    assert "5" in result[0]["reason"]


def test_recommendation_for_missing_agent_telemetry():
    queue = {
        "state": {"code": "ready_to_apply"},
        "ats": {"approved": 12, "leaseable": 12, "dedup_blocked": 0},
        "linkedin": {"queued": 0, "canary_exhausted": False},
    }
    browser = {"counts": {}}
    agents = {
        "verdict": {"code": "telemetry_missing"},
        "workers": [{"worker_id": "m2-0", "sw_version": "0.3.0+git.old"}],
    }

    result = console_diagnosis.recommendations_from(queue, browser, agents=agents)

    assert result[0]["code"] == "redeploy_apply_workers_for_telemetry"
    assert result[0]["severity"] == "warn"
    assert "redeploy" in result[0]["reason"].lower()


def test_operational_rollups_include_machines_hosts_forecast_goals_and_workers(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat "
                "(worker_id, machine_owner, home_ip, role, state, last_beat, current_agent, current_model) "
                "VALUES "
                "('m4-0','m4','100.69.68.103','apply','idle',now(),'codex','sonnet'), "
                "('m4-score-0','m4','0.0.0.0','compute','idle',now(),NULL,NULL), "
                "('m2-disc-0','m2','100.77.65.8','discovery','idle',now(),NULL,NULL), "
                "('m2-disc',NULL,'100.77.65.8','discovery','idle',now(),NULL,NULL), "
                "('home-linkedin-0',NULL,'100.90.104.99','linkedin','idle',now(),NULL,NULL), "
                "('watchdog',NULL,'100.90.104.99','watchdog','idle',now() - interval '10 minutes',NULL,NULL)"
            )
            cur.execute(
                "INSERT INTO apply_queue "
                "(url, company, title, application_url, score, lane, status, approved_batch, "
                "dedup_key, target_host, apply_domain, worker_id, est_cost_usd, updated_at) "
                "VALUES "
                "('https://boards.greenhouse.io/acme/jobs/1','Acme','Engineer',"
                "'https://boards.greenhouse.io/acme/jobs/1/apply',8,'ats','applied','b',"
                "'acme::eng','boards.greenhouse.io','boards.greenhouse.io','m4-0',0.50,now()), "
                "('https://jobs.ashbyhq.com/beta/1','Beta','Analyst',"
                "'https://jobs.ashbyhq.com/beta/1/apply',8,'ats','failed','b',"
                "'beta::analyst','jobs.ashbyhq.com','jobs.ashbyhq.com','m4-0',0.25,now())"
            )
        conn.commit()

        result = console_diagnosis.operational_rollups(conn)

    assert result["machines"]["m4"]["workers"] == 2
    assert result["machines"]["m4"]["display_name"] == "GGGTower"
    assert result["machines"]["m4"]["roles"]["apply"] == 1
    assert result["machines"]["m2"]["display_name"] == "TARPON"
    assert result["machines"]["m2"]["workers"] == 2
    assert result["machines"]["home"]["display_name"] == "Home"
    assert result["machines"]["home"]["workers"] == 2
    assert result["machines"]["home"]["stale_workers"] == 1
    assert "(unknown)" not in result["machines"]
    assert result["host_quality"][0]["host"] in {"boards.greenhouse.io", "jobs.ashbyhq.com"}
    assert result["throughput"]["applied_24h"] == 1
    assert result["daily_goal"]["configured"] is False
    assert result["worker_comparison"][0]["worker_id"] == "m4-0"
    assert result["worker_comparison"][0]["success_rate"] == 0.5
    assert result["worker_comparison"][0]["crash_rate"] == 0.0
    assert result["worker_comparison"][0]["cost_per_applied"] == 0.75
    assert result["freshness"]["last_apply_at"] is not None


def test_operational_rollups_tolerate_missing_agent_heartbeat_columns(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE worker_heartbeat "
                "DROP COLUMN current_agent, "
                "DROP COLUMN current_model"
            )
            cur.execute(
                "INSERT INTO worker_heartbeat "
                "(worker_id, machine_owner, home_ip, role, state, last_beat) "
                "VALUES ('m4-legacy','m4','100.69.68.103','apply','idle',now())"
            )
        conn.commit()

        result = console_diagnosis.operational_rollups(conn)

    assert result["machines"]["m4"]["workers"] == 1
    assert result["machines"]["m4"]["roles"]["apply"] == 1
