from applypilot.apply import pgqueue
from applypilot.fleet import queue_diagnosis


def _seed_apply_row(
    cur,
    *,
    url,
    status="queued",
    company="Acme",
    title="Analyst",
    dedup_key="dk",
    approved=True,
    apply_error=None,
    apply_status=None,
):
    cur.execute(
        "INSERT INTO apply_queue "
        "(url, application_url, score, status, lane, apply_domain, target_host, "
        "dedup_key, approved_batch, company, title, apply_error, apply_status) "
        "VALUES (%s,%s,9,%s,'ats','boards.greenhouse.io','boards.greenhouse.io',"
        "%s,%s,%s,%s,%s,%s)",
        (
            url,
            f"https://example.test/{url}",
            status,
            dedup_key,
            "batch-1" if approved else None,
            company,
            title,
            apply_error,
            apply_status,
        ),
    )


def test_queue_diagnosis_counts_base_leaseable_and_dedup_blocked_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        _seed_apply_row(cur, url="leaseable", dedup_key="dk-open")
        _seed_apply_row(cur, url="unapproved", dedup_key="dk-unapproved", approved=False)
        _seed_apply_row(cur, url="blocked-stale", company="HiringCafe", dedup_key="dk-board")
        cur.execute(
            "INSERT INTO applied_set (dedup_key, company) VALUES ('dk-board','HiringCafe')"
        )
        _seed_apply_row(
            cur,
            url="crash-source",
            status="crash_unconfirmed",
            dedup_key="dk-crash",
            apply_error="failed:timeout",
        )
        _seed_apply_row(cur, url="blocked-crash", dedup_key="dk-crash")
        cur.execute(
            "INSERT INTO applied_set (dedup_key, company) VALUES ('dk-crash','Acme')"
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        result = queue_diagnosis.queue_diagnosis(conn)

    assert result["queued"]["total"] == 4
    assert result["queued"]["approved_ats"] == 3
    assert result["queued"]["dedup_blocked"] == 2
    assert result["queued"]["base_leaseable"] == 1
    assert result["dedup"]["blocked_sources"]["has_crash_source"] == 1
    assert result["dedup"]["blocked_sources"]["no_current_queue_source"] == 1
    assert result["dedup"]["overbroad_groups"][0]["dedup_key"] == "dk-board"
    assert result["dedup"]["overbroad_groups"][0]["queued_rows"] == 1


def test_queue_diagnosis_exposes_blocked_reason_breakdown(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        _seed_apply_row(cur, url="unsupported", status="blocked", apply_error="exception_pending")
        cur.execute(
            "UPDATE apply_queue SET host_policy='adapter_unsupported', execution_route='exception' "
            "WHERE url='unsupported'"
        )
        _seed_apply_row(cur, url="expired", status="blocked", apply_error="expired")
        cur.execute(
            "UPDATE apply_queue SET host_policy='adapter_ready', execution_route='deterministic' "
            "WHERE url='expired'"
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        result = queue_diagnosis.queue_diagnosis(conn)

    assert result["blocked"] == {
        "total": 2,
        "groups": {"routing_or_policy": 1, "unavailable": 1},
        "reasons": {"adapter_unsupported": 1, "expired": 1},
    }


def test_queue_diagnosis_separates_skipped_challenges_from_active_walls(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        _seed_apply_row(
            cur,
            url="skipped-wall",
            status="blocked",
            apply_error="challenge_pending",
            apply_status="challenge_skipped",
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        result = queue_diagnosis.queue_diagnosis(conn)

    assert result["blocked"]["reasons"] == {"challenge_skipped": 1}
    assert result["blocked"]["groups"] == {"operator_skipped": 1}


def test_queue_diagnosis_exposes_terminal_reason_breakdown(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        _seed_apply_row(cur, url="timeout", status="failed", apply_error="failed:timeout")
        _seed_apply_row(
            cur,
            url="uncertain",
            status="crash_unconfirmed",
            apply_error="crash_unconfirmed",
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        result = queue_diagnosis.queue_diagnosis(conn)

    assert result["terminal"] == {
        "total": 2,
        "groups": {"submission_uncertain": 1, "timeout_or_stuck": 1},
        "reasons": {"crash_unconfirmed": 1, "failed:timeout": 1},
    }
