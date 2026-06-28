from applypilot.apply import pgqueue


def _seed_li(conn, n, *, batch="b1", approved=True):
    with conn.cursor() as cur:
        for i in range(n):
            cur.execute("INSERT INTO linkedin_queue (url, application_url, score, status, lane, approved_batch, dedup_key) "
                        "VALUES (%s,%s,%s,'queued','ats',%s,%s)",
                        (f"li{i}", f"https://linkedin.com/jobs/{i}", 9.0-i*0.01, batch if approved else None, f"dk{i}"))
        conn.commit()


def test_linkedin_lease_halt_blocks(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_li(conn, 1)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO rate_governor (scope_key, halted_until) VALUES ('account:linkedin', now() + interval '1 hour') "
                        "ON CONFLICT (scope_key) DO UPDATE SET halted_until=EXCLUDED.halted_until")
        conn.commit()
        assert queue.lease_linkedin(conn, "w1", public_ip="1.1.1.1", owner_ip="1.1.1.1") is None


def test_linkedin_lease_dedup_blocks(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_li(conn, 1)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO applied_set (dedup_key, company) VALUES ('dk0','Acme')")
        conn.commit()
        assert queue.lease_linkedin(conn, "w1", public_ip="1.1.1.1", owner_ip="1.1.1.1") is None  # already applied


def test_linkedin_canary_caps(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_li(conn, 3)
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET linkedin_canary_enabled=TRUE, linkedin_canary_remaining=1 WHERE id=1")
            cur.execute("UPDATE rate_governor SET min_gap_seconds=0 WHERE scope_key='account:linkedin'")  # not yet created
        conn.commit()
        a = queue.lease_linkedin(conn, "w1", public_ip="1.1.1.1", owner_ip="1.1.1.1", min_gap_seconds=0)
        b = queue.lease_linkedin(conn, "w2", public_ip="1.1.1.1", owner_ip="1.1.1.1", min_gap_seconds=0)
    assert a is not None and b is None  # canary capped at 1
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT linkedin_canary_remaining FROM fleet_config WHERE id=1")
        assert cur.fetchone()["linkedin_canary_remaining"] == 0


def test_linkedin_min_gap_default_is_ttl(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_li(conn, 1)
        queue.lease_linkedin(conn, "w1", public_ip="1.1.1.1", owner_ip="1.1.1.1")  # creates the account row
        with conn.cursor() as cur:
            cur.execute("SELECT min_gap_seconds FROM rate_governor WHERE scope_key='account:linkedin'")
            assert cur.fetchone()["min_gap_seconds"] == 1200


def test_linkedin_schema_columns(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT linkedin_canary_enabled, linkedin_canary_remaining FROM fleet_config WHERE id=1")
        row = cur.fetchone()
        assert row["linkedin_canary_enabled"] is False and row["linkedin_canary_remaining"] is None
        cur.execute("INSERT INTO rate_governor (scope_key) VALUES ('account:linkedin')")
        cur.execute("SELECT halted_until FROM rate_governor WHERE scope_key='account:linkedin'")
        assert cur.fetchone()["halted_until"] is None
