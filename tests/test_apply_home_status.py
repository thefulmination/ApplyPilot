from applypilot.apply import pgqueue
from applypilot.fleet import apply_home_main


def test_print_status_includes_queue_diagnosis(fleet_db, capsys):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, score, status, lane, apply_domain, target_host, "
            "dedup_key, approved_batch, company, title) "
            "VALUES ('q1','https://example.test/q1',9,'queued','ats','boards.greenhouse.io',"
            "'boards.greenhouse.io','dk1','batch-1','HiringCafe','Chief of Staff')"
        )
        cur.execute("INSERT INTO applied_set (dedup_key, company) VALUES ('dk1','HiringCafe')")
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        apply_home_main._print_status(conn)

    out = capsys.readouterr().out
    assert "queue_diagnosis" in out
    assert "dedup_blocked" in out
