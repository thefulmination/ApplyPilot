# tests/test_fleet_discovery.py
from applypilot.apply import pgqueue
from applypilot.fleet import queue


def test_push_discovered_stages_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        n = queue.push_discovered(conn, task_id="t1", source_label="chief of staff", worker_id="w1",
                                  postings=[{"job_url": "u1", "title": "COS"}, {"job_url": "u2", "title": "PM"}])
        assert n == 2
        with conn.cursor() as cur:
            cur.execute("SELECT task_id, source_label, worker_id, posting, synced_to_home_at "
                        "FROM discovered_postings ORDER BY posting->>'job_url'")
            rows = cur.fetchall()
        assert [r["posting"]["job_url"] for r in rows] == ["u1", "u2"]
        assert rows[0]["task_id"] == "t1" and rows[0]["source_label"] == "chief of staff"
        assert rows[0]["synced_to_home_at"] is None
