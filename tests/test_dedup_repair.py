import json

from applypilot.apply import pgqueue
from applypilot.fleet import dedup_repair, dedup_repair_main


def _seed_row(
    cur,
    *,
    url,
    dedup_key,
    company="HiringCafe",
    title="Chief of Staff",
    application_url=None,
    status="queued",
):
    cur.execute(
        "INSERT INTO apply_queue "
        "(url, application_url, score, status, lane, apply_domain, target_host, "
        "dedup_key, approved_batch, company, title) "
        "VALUES (%s,%s,9,%s,'ats','boards.greenhouse.io','boards.greenhouse.io',"
        "%s,'batch-1',%s,%s)",
        (
            url,
            application_url or f"https://boards.greenhouse.io/acme/jobs/{url}",
            status,
            dedup_key,
            company,
            title,
        ),
    )


def test_plan_repair_detects_overbroad_group_and_proposes_distinct_keys(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        _seed_row(
            cur,
            url="job-a",
            dedup_key="dk-board",
            company="HiringCafe",
            application_url="https://boards.greenhouse.io/acme/jobs/1",
        )
        _seed_row(
            cur,
            url="job-b",
            dedup_key="dk-board",
            company="HiringCafe",
            application_url="https://jobs.ashbyhq.com/beta/2",
        )
        cur.execute("INSERT INTO applied_set (dedup_key, company) VALUES ('dk-board','HiringCafe')")
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        plan = dedup_repair.plan_repair(conn, dedup_key="dk-board")

    assert plan["dedup_key"] == "dk-board"
    assert plan["safe_to_apply"] is True
    assert len(plan["candidates"]) == 2
    new_keys = {c["new_dedup_key"] for c in plan["candidates"]}
    assert len(new_keys) == 2
    assert "dk-board" not in new_keys
    assert all(c["reason"] == "source_specific_overbroad_key" for c in plan["candidates"])


def test_execute_repair_updates_queued_rows_and_writes_audit(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        _seed_row(
            cur,
            url="job-a",
            dedup_key="dk-board",
            application_url="https://boards.greenhouse.io/acme/jobs/1",
        )
        _seed_row(
            cur,
            url="job-b",
            dedup_key="dk-board",
            application_url="https://jobs.ashbyhq.com/beta/2",
        )
        _seed_row(
            cur,
            url="job-done",
            dedup_key="dk-board",
            application_url="https://jobs.ashbyhq.com/gamma/3",
            status="applied",
        )
        cur.execute("INSERT INTO applied_set (dedup_key, company) VALUES ('dk-board','HiringCafe')")
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        result = dedup_repair.execute_repair(conn, dedup_key="dk-board", max_rows=2, reason="test")

    assert result["updated"] == 2
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT url, dedup_key FROM apply_queue ORDER BY url")
        rows = {r["url"]: r["dedup_key"] for r in cur.fetchall()}
        assert rows["job-a"] != "dk-board"
        assert rows["job-b"] != "dk-board"
        assert rows["job-done"] == "dk-board"
        cur.execute("SELECT COUNT(*) AS n, MIN(how_to_reverse) AS reverse FROM dedup_repair_actions")
        audit = cur.fetchone()
        assert audit["n"] == 2
        assert "UPDATE apply_queue" in audit["reverse"]
        assert "dk-board" in audit["reverse"]
        cur.execute("SELECT how_to_reverse FROM dedup_repair_actions ORDER BY url LIMIT 1")
        assert "UPDATE apply_queue SET dedup_key='dk-board'" in cur.fetchone()["how_to_reverse"]


def test_execute_repair_refuses_specific_company_key(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        _seed_row(cur, url="job-a", dedup_key="dk-acme", company="Acme")
        cur.execute("INSERT INTO applied_set (dedup_key, company) VALUES ('dk-acme','Acme')")
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        result = dedup_repair.execute_repair(conn, dedup_key="dk-acme", max_rows=1, reason="test")

    assert result["updated"] == 0
    assert result["refused_reason"] == "dedup_key_is_not_overbroad"


def test_plan_repair_refuses_key_not_in_applied_set(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        _seed_row(cur, url="job-a", dedup_key="dk-not-blocked", company="HiringCafe")
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        plan = dedup_repair.plan_repair(conn, dedup_key="dk-not-blocked")

    assert plan["safe_to_apply"] is False
    assert plan["refused_reason"] == "dedup_key_not_in_applied_set"


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_dedup_repair_cli_json_dry_run(monkeypatch, capsys):
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn=None: _Conn())
    monkeypatch.setattr(
        dedup_repair,
        "plan_repair",
        lambda conn, dedup_key, limit=25: {
            "dedup_key": dedup_key,
            "safe_to_apply": True,
            "candidates": [{"url": "u1", "new_dedup_key": "new"}],
        },
    )

    rc = dedup_repair_main.main(["--dsn", "pg", "--dedup-key", "dk-board", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dedup_key"] == "dk-board"
    assert payload["candidates"][0]["url"] == "u1"
