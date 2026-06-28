# tests/test_fleet_linkedin_home.py
from applypilot.apply import pgqueue


def test_linkedin_approve_gated_by_canary(fleet_db):
    from applypilot.fleet import linkedin_home_main as hm, queue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO linkedin_queue (url, application_url, score, status, lane) "
                    "VALUES ('q1','https://linkedin.com/jobs/1','9','queued','ats')")
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
        queue.push_linkedin_jobs(conn, [{"url": "p1", "company": "Acme", "title": "COS",
                                         "application_url": "https://linkedin.com/jobs/1", "score": 9}], approved_batch=None)
        cur.execute("SELECT dedup_key, lane FROM linkedin_queue WHERE url='p1'")
        r = cur.fetchone()
        assert r["dedup_key"] == _dedup.dedup_key("Acme", "COS")  # same key as offsite -> cross-lane dedup
