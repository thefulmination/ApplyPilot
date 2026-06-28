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


from applypilot.fleet import governor


def test_park_linkedin_sets_halt_one_tx_even_without_account_row(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO linkedin_queue (url, application_url, score, status, lane, lease_owner) "
                    "VALUES ('lp','https://linkedin.com/jobs/x','9','leased','ats','w1')")
        conn.commit()
        assert queue.park_linkedin_challenge(conn, "w1", "lp", halt_seconds=21600) is True
        cur.execute("SELECT halted_until FROM rate_governor WHERE scope_key='account:linkedin'")
        assert cur.fetchone()["halted_until"] is not None       # account row was INSERTed + halted
        cur.execute("SELECT status, apply_status FROM linkedin_queue WHERE url='lp'")
        r = cur.fetchone(); assert r["status"] == "leased" and r["apply_status"] == "challenge_pending"  # frozen, not closed


def test_reclaim_linkedin_crash_unconfirmed_only(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO linkedin_queue (url, application_url, score, status, lane, lease_owner, lease_expires_at, attempts) "
                    "VALUES ('lr','https://linkedin.com/jobs/y','9','leased','ats','wDead', now()-interval '5 min', 1)")
        conn.commit()
        assert queue.reclaim_linkedin(conn) == 1
        cur.execute("SELECT status, attempts FROM linkedin_queue WHERE url='lr'")
        r = cur.fetchone(); assert r["status"] == "crash_unconfirmed" and r["attempts"] == 99  # NEVER re-queued


def test_clear_and_kill_halt(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        queue.kill_linkedin(conn)
        cur.execute("SELECT halted_until > now() + interval '300 days' AS far FROM rate_governor WHERE scope_key='account:linkedin'")
        assert cur.fetchone()["far"] is True
        queue.clear_linkedin_halt(conn)
        cur.execute("SELECT halted_until FROM rate_governor WHERE scope_key='account:linkedin'")
        assert cur.fetchone()["halted_until"] is None


def test_linkedin_interlock_refuses_when_held(fleet_db):
    from applypilot.fleet import linkedin_worker_main as lm
    from applypilot.apply import pgqueue
    holder = pgqueue.connect(fleet_db)  # a separate session holds the lock
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext('applypilot:linkedin_driver'))")
        holder.commit()
        with pgqueue.connect(fleet_db) as conn:
            assert lm.acquire_linkedin_interlock(conn) is False  # already held -> refuse
    finally:
        holder.close()


def test_build_linkedin_loop_role(fleet_db):
    from applypilot.fleet import linkedin_worker_main as lm
    loop = lm.build_linkedin_loop(dsn=fleet_db, worker_id="w1", owner_ip="1.1.1.1", model="sonnet", agent="claude")
    assert loop.role == "linkedin" and loop.apply_fn is not None and loop.owner_ip == "1.1.1.1"
