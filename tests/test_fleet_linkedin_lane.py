import os
from datetime import datetime, timedelta, timezone

from applypilot.apply import pgqueue


def _seed_li(conn, n, *, batch="b1", approved=True):
    policy = "test-linkedin-policy"
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_decision_policies (policy_version,lane,status) "
            "VALUES (%s,'linkedin','active') ON CONFLICT (policy_version) DO UPDATE SET status='active'",
            (policy,),
        )
        cur.execute(
            "UPDATE fleet_config SET linkedin_policy_version=%s, approval_threshold=0 WHERE id=1",
            (policy,),
        )
        for i in range(n):
            score = 9.0 - i * 0.01
            cur.execute(
                "INSERT INTO linkedin_queue (url, application_url, score, status, lane, approved_batch, dedup_key, "
                "decision_id,policy_version,decision_action,qualification_verdict,qualification_score,qualification_floor,"
                "preference_score,outcome_score,final_score,decision_confidence,decision_created_at,decision_expires_at,input_hash) "
                "VALUES (%s,%s,%s,'queued','linkedin',%s,%s,%s,%s,'apply','qualified',9,7,8,8,%s,.9,%s,%s,%s)",
                (f"li{i}", f"https://linkedin.com/jobs/{i}", score, batch if approved else None,
                 f"dk{i}", f"li-d{i}", policy, score, now, now + timedelta(days=1), f"li-h{i}"),
            )
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


def test_linkedin_should_halt_ignores_apply_queue_spend_cap(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, est_cost_usd) "
            "VALUES ('ats-spend', 'https://example.com/apply', 9, 'applied', 2.0)"
        )
        conn.commit()

        pgqueue.set_spend_cap(conn, 1.0)
        assert pgqueue.should_halt(conn) is True
        assert pgqueue.linkedin_should_halt(conn) is False

        pgqueue.set_paused(conn, True)
        assert pgqueue.linkedin_should_halt(conn) is True


def test_linkedin_external_auth_required_closes_job_without_account_halt(fleet_db):
    from applypilot.fleet.worker import WorkerLoop

    with pgqueue.connect(fleet_db) as conn:
        _seed_li(conn, 1)

    def external_auth(_job):
        return {
            "run_status": "auth_required",
            "est_cost_usd": 0.02,
            "apply_channel": "external",
            "apply_external_host": "workdayjobs.com",
        }

    loop = WorkerLoop(
        lambda: pgqueue.connect(fleet_db),
        "w-ext-auth",
        home_ip="1.1.1.1",
        role="linkedin",
        public_ip="1.1.1.1",
        owner_ip="1.1.1.1",
        apply_fn=external_auth,
    )

    assert loop.run_once() == {"action": "external_auth_required", "url": "li0"}

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status::text, apply_status, apply_error, apply_channel, apply_external_host "
            "FROM linkedin_queue WHERE url='li0'"
        )
        row = cur.fetchone()
        assert row["status"] == "failed"
        assert row["apply_status"] == "auth_required"
        assert row["apply_error"] == "external_auth_required:workdayjobs.com"
        assert row["apply_channel"] == "external"
        assert row["apply_external_host"] == "workdayjobs.com"

        cur.execute("SELECT count(*) AS n FROM auth_challenge WHERE resolved_at IS NULL")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT halted_until FROM rate_governor WHERE scope_key='account:linkedin'")
        governor = cur.fetchone()
        assert governor is not None and governor["halted_until"] is None


def test_linkedin_driver_uses_lane_specific_halt(monkeypatch):
    from applypilot.fleet import linkedin_worker_main as lm

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Loop:
        def run_once(self):
            return {"action": "idle"}

    calls = []

    def _legacy_should_halt(_conn):
        raise AssertionError("LinkedIn driver must not read the ATS/apply halt path")

    def _linkedin_should_halt(_conn):
        calls.append("linkedin")
        return False

    monkeypatch.setattr(pgqueue, "should_halt", _legacy_should_halt)
    monkeypatch.setattr(pgqueue, "linkedin_should_halt", _linkedin_should_halt, raising=False)

    counts = lm.run_linkedin(lambda: _Conn(), _Loop(), max_iterations=1, idle_sleep=0)
    assert counts["error"] == 0
    assert calls == ["linkedin"]


def test_linkedin_loop_defaults_to_codex_agent(monkeypatch):
    from applypilot.fleet import apply_worker_main as awm
    from applypilot.fleet import linkedin_worker_main as lm

    captured = {}

    def _fake_make_apply_fn(model, agent):
        captured["model"] = model
        captured["agent"] = agent
        return lambda job: {"run_status": "failed:usage_limit", "est_cost_usd": 0.0}

    monkeypatch.setattr(awm, "make_apply_fn", _fake_make_apply_fn)

    lm.build_linkedin_loop(dsn="postgresql://example.invalid/db", worker_id="w", owner_ip="1.1.1.1")

    assert captured["agent"] == "codex"


def test_linkedin_driver_backs_off_after_usage_limit(monkeypatch):
    from applypilot.fleet import linkedin_worker_main as lm

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Loop:
        def run_once(self):
            return {"action": "usage_limit", "url": "li-wall"}

    sleeps = []
    monkeypatch.setattr(pgqueue, "linkedin_should_halt", lambda conn: False)
    monkeypatch.setattr(lm.time, "sleep", lambda seconds: sleeps.append(seconds))

    counts = lm.run_linkedin(lambda: _Conn(), _Loop(), max_iterations=1, idle_sleep=7)
    assert counts["idle"] == 1
    assert sleeps == [7]


def test_linkedin_driver_switches_to_fallback_after_usage_limit(monkeypatch):
    from applypilot.fleet import linkedin_worker_main as lm
    from applypilot.fleet.agent_switch import AgentSwitcher

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    actions = iter(["usage_limit", "applied"])

    class _Loop:
        _log_tail_fn = None

        def run_once(self):
            return {"action": next(actions), "url": "li-wall"}

    rebuilt = []

    def rebuild(agent):
        rebuilt.append(agent)
        return lambda job: {}

    monkeypatch.setattr(pgqueue, "linkedin_should_halt", lambda conn: False)
    sw = AgentSwitcher(agents=["codex", "claude"], cooldown_seconds=3600)

    counts = lm.run_linkedin(
        lambda: _Conn(),
        _Loop(),
        max_iterations=2,
        idle_sleep=0,
        switcher=sw,
        rebuild_apply_fn=rebuild,
        time_fn=lambda: 1000.0,
    )

    assert rebuilt[0] == "codex"
    assert "claude" in rebuilt
    assert sw.blocked_until("codex") == 4600.0
    assert counts["applied"] == 1


def test_linkedin_driver_pauses_when_all_agents_walled(monkeypatch):
    from applypilot.fleet import linkedin_worker_main as lm
    from applypilot.fleet.agent_switch import AgentSwitcher

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Loop:
        _log_tail_fn = None

        def __init__(self):
            self.ran = 0
            self.beats = []

        def _beat(self, _conn, state):
            self.beats.append(state)

        def run_once(self):
            self.ran += 1
            return {"action": "applied", "url": "li"}

    monkeypatch.setattr(pgqueue, "linkedin_should_halt", lambda conn: False)
    sw = AgentSwitcher(agents=["codex", "claude"], cooldown_seconds=3600)
    sw.note_wall("codex", now=1000.0)
    sw.note_wall("claude", now=1000.0)

    loop = _Loop()
    counts = lm.run_linkedin(
        lambda: _Conn(),
        loop,
        max_iterations=2,
        idle_sleep=0,
        switcher=sw,
        rebuild_apply_fn=lambda agent: (lambda job: {}),
        time_fn=lambda: 1000.0,
    )

    assert loop.ran == 0
    assert loop.beats == ["paused", "paused"]
    assert counts["idle"] == 2


def test_linkedin_setup_env_prefers_repo_local_profile(monkeypatch, tmp_path):
    from applypilot.fleet import linkedin_worker_main as lm

    fake_module = tmp_path / "src" / "applypilot" / "fleet" / "linkedin_worker_main.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("", encoding="utf-8")
    repo_app = tmp_path / ".applypilot"
    repo_app.mkdir()
    for name in (
        "profile.json",
        "resume.txt",
        "resume.pdf",
        "resume_strategy.yaml",
        "job_preference_profile.json",
        "job_knowledge_graph_prompt.md",
    ):
        (repo_app / name).write_text("x", encoding="utf-8")

    for name in (
        "APPLYPILOT_DIR",
        "APPLYPILOT_PROFILE_PATH",
        "APPLYPILOT_RESUME_PATH",
        "APPLYPILOT_RESUME_PDF_PATH",
        "APPLYPILOT_RESUME_STRATEGY_PATH",
        "APPLYPILOT_PREFERENCE_PROFILE_PATH",
        "APPLYPILOT_KNOWLEDGE_GRAPH_PROMPT_PATH",
        "APPLYPILOT_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(lm, "__file__", str(fake_module))

    lm._setup_apply_env()

    assert os.environ["APPLYPILOT_DIR"] == str(repo_app)
    assert os.environ["APPLYPILOT_PROFILE_PATH"] == str(repo_app / "profile.json")
    assert os.environ["APPLYPILOT_RESUME_PATH"] == str(repo_app / "resume.txt")
    assert os.environ["APPLYPILOT_RESUME_PDF_PATH"] == str(repo_app / "resume.pdf")
    assert os.environ["APPLYPILOT_DB_PATH"].endswith("fleet_apply_throwaway.db")


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
        r = cur.fetchone()
        assert r["status"] == "leased" and r["apply_status"] == "challenge_pending"  # frozen, not closed


def test_reclaim_linkedin_crash_unconfirmed_only(fleet_db):
    from applypilot.fleet import queue

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO linkedin_queue (url, application_url, score, status, lane, lease_owner, lease_expires_at, attempts) "
                    "VALUES ('lr','https://linkedin.com/jobs/y','9','leased','ats','wDead', now()-interval '5 min', 1)")
        conn.commit()
        assert queue.reclaim_linkedin(conn) == 1
        cur.execute("SELECT status, attempts FROM linkedin_queue WHERE url='lr'")
        r = cur.fetchone()
        assert r["status"] == "crash_unconfirmed" and r["attempts"] == 99  # NEVER re-queued


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


def test_supervised_detects_fleet_linkedin(fleet_db):
    from applypilot.apply import launcher, pgqueue
    holder = pgqueue.connect(fleet_db)
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext('applypilot:linkedin_driver'))")
        holder.commit()
        assert launcher.fleet_linkedin_active(fleet_db) is True   # fleet holds it
    finally:
        holder.close()
    assert launcher.fleet_linkedin_active(fleet_db) is False      # lock free now
