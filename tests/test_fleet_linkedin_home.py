# tests/test_fleet_linkedin_home.py
from datetime import datetime, timedelta, timezone

from applypilot.apply import pgqueue


def test_linkedin_challenge_surfaces_exclude_shared_url_not_parked_in_linkedin(fleet_db, capsys):
    from applypilot.fleet import linkedin_home_main as hm

    url = "https://shared.example/jobs/linkedin-status-collision"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, score, status, apply_status, lane, lease_owner, lease_expires_at) "
            "VALUES (%s,%s,9,'leased','challenge_pending','ats','ats-worker',now()+interval '3650 days')",
            (url, url),
        )
        cur.execute(
            "INSERT INTO linkedin_queue (url, application_url, score, status, lane, apply_status) "
            "VALUES (%s,%s,9,'queued','linkedin',NULL)",
            (url, url),
        )
        cur.execute(
            "INSERT INTO auth_challenge (url, worker_id, kind, route) "
            "VALUES (%s,'ats-worker','login_gate','owner_inbox')",
            (url,),
        )
        conn.commit()

        assert hm.list_challenges(conn) == []
        hm._print_status(conn)
    assert "'open_challenges': 0" in capsys.readouterr().out


def test_linkedin_approve_gated_by_canary(fleet_db):
    from applypilot.fleet import linkedin_home_main as hm
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO linkedin_queue "
            "(url, application_url, score, status, lane, linkedin_resolve_status, linkedin_resolved_at) "
            "VALUES ('q1','https://linkedin.com/jobs/1','9','queued','ats','easy_apply',now())"
        )
        conn.commit()
        try:
            hm.approve(conn, all_pushed=True); assert False, "must refuse without canary"
        except SystemExit:
            pass
        hm.set_linkedin_canary(conn, 1)
        token = hm.approve(conn, all_pushed=True)
        cur.execute("SELECT approved_batch FROM linkedin_queue WHERE url='q1'")
        assert cur.fetchone()["approved_batch"] == token


def test_push_linkedin_jobs_dedup_key(fleet_db):
    from applypilot.fleet import queue, dedup as _dedup
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_decision_policies (policy_version,lane,status) "
            "VALUES ('li-test-policy','linkedin','active')"
        )
        now = datetime.now(timezone.utc)
        queue.push_linkedin_jobs(conn, [{"url": "p1", "company": "Acme", "title": "COS",
                                         "application_url": "https://linkedin.com/jobs/1", "score": 9,
                                         "decision_id": "d1", "policy_version": "li-test-policy",
                                         "decision_action": "apply", "qualification_verdict": "qualified",
                                         "qualification_score": 9.0, "qualification_floor": 7.0,
                                         "preference_score": 8.0, "outcome_score": 8.0,
                                         "final_score": 9.0, "decision_confidence": 0.9,
                                         "decision_created_at": now,
                                         "decision_expires_at": now + timedelta(days=1),
                                         "input_hash": "hash-d1"}], approved_batch=None)
        cur.execute("SELECT dedup_key, lane FROM linkedin_queue WHERE url='p1'")
        r = cur.fetchone()
        assert r["dedup_key"] == _dedup.dedup_key("Acme", "COS")  # same key as offsite -> cross-lane dedup
