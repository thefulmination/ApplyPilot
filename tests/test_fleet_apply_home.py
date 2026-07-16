"""Task 6: double-apply backfill -- seed PG applied_set from home brain apply history.

Tests that backfill_applied_set:
  - reads jobs.apply_status='applied' + apply_error in ('no_confirmation','crash_unconfirmed')
  - reads applications ledger rows with status='applied' (joined to jobs for company/title)
  - inserts into PG applied_set (dedup_key, company) ON CONFLICT DO NOTHING
  - is idempotent (second run returns >= 0, no crash, no duplicates)
"""
import sqlite3
from applypilot.apply import pgqueue


def _home_sqlite(tmp_path):
    db = tmp_path / "home.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT, "
        "apply_status TEXT, apply_error TEXT, audit_score REAL, fit_score REAL, full_description TEXT, "
        "liveness_status TEXT, duplicate_of_url TEXT, decision_source TEXT, "
        "fit_gap_category TEXT, recommended_action TEXT, audit_flags TEXT);"
        "CREATE TABLE applications (job_url TEXT, application_url TEXT, status TEXT);")
    return conn


def test_backfill_applied_set_from_home_history(fleet_db, tmp_path):
    from applypilot.fleet import sync
    sq = _home_sqlite(tmp_path)
    sq.execute("INSERT INTO jobs (url, company, title, apply_status) VALUES ('h1','Acme','COS','applied')")
    sq.execute("INSERT INTO applications (job_url, status) VALUES ('h2','applied')")  # ledger-only
    sq.execute("INSERT INTO jobs (url, company, title) VALUES ('h2','Beta','PM')")
    sq.commit()
    from applypilot.fleet import dedup as _dedup
    dk_acme = _dedup.dedup_key("Acme", "COS")
    dk_beta = _dedup.dedup_key("Beta", "PM")
    with pgqueue.connect(fleet_db) as pg:
        n = sync.backfill_applied_set(sq, pg)
        with pg.cursor() as cur:
            # jobs.apply_status='applied' source (Acme/COS)
            cur.execute("SELECT count(*) AS n FROM applied_set WHERE dedup_key = %s", (dk_acme,))
            assert cur.fetchone()["n"] == 1, "Acme/COS (jobs-history source) missing from applied_set"
            # applications-ledger-only source (Beta/PM — the load-bearing lost-apply_status case)
            cur.execute("SELECT count(*) AS n FROM applied_set WHERE dedup_key = %s", (dk_beta,))
            assert cur.fetchone()["n"] == 1, "Beta/PM (ledger-only source) missing from applied_set — backfill production bug"
    assert n >= 2
    # idempotent second run
    with pgqueue.connect(fleet_db) as pg:
        assert sync.backfill_applied_set(sq, pg) == 0


def test_apply_home_canary_and_approve_gate(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    from applypilot.apply import pgqueue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET canary_enabled=FALSE, canary_remaining=NULL WHERE id=1")
        cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, apply_domain) "
                    "VALUES ('q1','http://x','9','queued','ats','x.com')")  # unapproved
        conn.commit()
    # approve refuses when canary not armed
    with pgqueue.connect(fleet_db) as conn:
        try:
            hm.approve(conn, all_pushed=True)
            assert False, "approve must refuse when canary not armed"
        except SystemExit:
            pass
    # arm canary, then approve
    with pgqueue.connect(fleet_db) as conn:
        hm.set_canary(conn, 3)
        token = hm.approve(conn, all_pushed=True)
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT canary_enabled, canary_remaining FROM fleet_config WHERE id=1")
        assert cur.fetchone()["canary_remaining"] == 3
        cur.execute("SELECT approved_batch FROM apply_queue WHERE url='q1'")
        assert cur.fetchone()["approved_batch"] == token


def test_approve_refuses_zero_remaining_canary(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    from applypilot.apply import pgqueue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET ats_apply_mode='canary', canary_enabled=TRUE, "
            "canary_remaining=0, paused=TRUE, ats_paused=FALSE WHERE id=1"
        )
        cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, apply_domain) "
                    "VALUES ('zero-canary','http://x','9','queued','ats','x.com')")
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        try:
            hm.approve(conn, all_pushed=True)
            assert False, "approve must refuse when canary has no remaining capacity"
        except SystemExit:
            pass
        with conn.cursor() as cur:
            cur.execute("SELECT approved_batch FROM apply_queue WHERE url='zero-canary'")
            assert cur.fetchone()["approved_batch"] is None


def test_approve_all_pushed_respects_approval_threshold_and_canary_capacity(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    from applypilot.apply import pgqueue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET approval_threshold=7, ats_apply_mode='canary', "
            "canary_enabled=TRUE, canary_remaining=1, paused=FALSE, ats_paused=FALSE WHERE id=1"
        )
        cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, apply_domain) "
                    "VALUES ('high','http://x/high','9','queued','ats','x.com')")
        cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, apply_domain) "
                    "VALUES ('low','http://x/low','6.9','queued','ats','x.com')")
        cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, apply_domain) "
                    "VALUES ('second-high','http://x/second','8','queued','ats','x.com')")
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        token = hm.approve(conn, all_pushed=True)
        with conn.cursor() as cur:
            cur.execute("SELECT url, approved_batch FROM apply_queue ORDER BY url")
            rows = {r["url"]: r["approved_batch"] for r in cur.fetchall()}

    assert rows["high"] == token
    assert rows["low"] is None
    assert rows["second-high"] is None


def test_approve_all_pushed_respects_operator_threshold_below_seven(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    from applypilot.apply import pgqueue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET approval_threshold=6.5, ats_apply_mode='canary', "
            "canary_enabled=TRUE, canary_remaining=2, paused=FALSE, ats_paused=FALSE WHERE id=1"
        )
        cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, apply_domain) "
                    "VALUES ('at-floor','http://x/floor','6.5','queued','ats','x.com')")
        cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, apply_domain) "
                    "VALUES ('below-floor','http://x/below','6.4','queued','ats','x.com')")
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        token = hm.approve(conn, all_pushed=True)
        with conn.cursor() as cur:
            cur.execute("SELECT url, approved_batch FROM apply_queue ORDER BY url")
            rows = {r["url"]: r["approved_batch"] for r in cur.fetchall()}

    assert rows["at-floor"] == token
    assert rows["below-floor"] is None


def test_apply_cycle_refuses_to_rearm_while_globally_paused(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    from applypilot.apply import pgqueue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, apply_domain) "
                    "VALUES ('cycle1','http://x/1','9','queued','ats','x.com')")
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        assert hm.arm_canary_if_safe(conn, 3) is True
        hm.approve(conn, all_pushed=True)
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET canary_remaining=0, paused=TRUE WHERE id=1")
            cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, apply_domain) "
                        "VALUES ('cycle2','http://x/2','9','queued','ats','x.com')")
        conn.commit()

        assert hm.arm_canary_if_safe(conn, 3) is False
        with conn.cursor() as cur:
            cur.execute("SELECT canary_enabled, canary_remaining, paused FROM fleet_config WHERE id=1")
            cfg = cur.fetchone()
            assert cfg["canary_enabled"] is False
            assert cfg["canary_remaining"] == 0
            assert cfg["paused"] is True
            cur.execute("SELECT approved_batch FROM apply_queue WHERE url='cycle2'")
            assert cur.fetchone()["approved_batch"] is None


def test_arm_canary_if_safe_refuses_ats_paused(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    from applypilot.apply import pgqueue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET canary_enabled=FALSE, canary_remaining=NULL, "
                    "paused=TRUE, ats_paused=TRUE, ats_pause_source='doctor' WHERE id=1")
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        assert hm.arm_canary_if_safe(conn, 3) is False
        with conn.cursor() as cur:
            cur.execute("SELECT canary_enabled, canary_remaining, paused, ats_paused, ats_pause_source "
                        "FROM fleet_config WHERE id=1")
            cfg = cur.fetchone()
        assert cfg["canary_enabled"] is False
        assert cfg["canary_remaining"] == 0
        assert cfg["paused"] is True
        assert cfg["ats_paused"] is True
        assert cfg["ats_pause_source"] == "doctor"
        try:
            hm.approve(conn, all_pushed=True)
            assert False, "approve must still refuse when canary remains disarmed"
        except SystemExit:
            pass


def test_manual_canary_refuses_ats_paused(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    from applypilot.apply import pgqueue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET canary_enabled=FALSE, canary_remaining=NULL, "
                    "paused=TRUE, ats_paused=TRUE, ats_pause_source='operator' WHERE id=1")
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        try:
            hm.set_canary(conn, 3)
            assert False, "manual canary must not bypass an ATS pause"
        except RuntimeError as exc:
            assert "ATS pause" in str(exc)
        with conn.cursor() as cur:
            cur.execute("SELECT canary_enabled, canary_remaining, paused, ats_paused, ats_pause_source "
                        "FROM fleet_config WHERE id=1")
            cfg = cur.fetchone()
        assert cfg["canary_enabled"] is False
        assert cfg["canary_remaining"] is None
        assert cfg["paused"] is True
        assert cfg["ats_paused"] is True
        assert cfg["ats_pause_source"] == "operator"


def test_canary_never_clears_an_existing_pause(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    from applypilot.apply import pgqueue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET canary_enabled=FALSE, canary_remaining=NULL, "
                    "paused=TRUE, ats_paused=FALSE WHERE id=1")
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        hm.set_canary(conn, 3)
        with conn.cursor() as cur:
            cur.execute("SELECT canary_enabled, canary_remaining, paused FROM fleet_config WHERE id=1")
            cfg = cur.fetchone()
        assert cfg["canary_enabled"] is True
        assert cfg["canary_remaining"] == 3
        assert cfg["paused"] is True


def test_arm_canary_if_safe_refuses_cost_cap(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    from applypilot.apply import pgqueue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET canary_enabled=FALSE, canary_remaining=NULL, "
                    "paused=TRUE, ats_paused=FALSE, cost_cap_daily_usd=1 WHERE id=1")
        cur.execute("INSERT INTO llm_usage (cost_usd, ts) VALUES (5, now())")
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        assert hm.arm_canary_if_safe(conn, 3) is False
        with conn.cursor() as cur:
            cur.execute("SELECT canary_enabled, canary_remaining, paused FROM fleet_config WHERE id=1")
            cfg = cur.fetchone()
        assert cfg["canary_enabled"] is False
        assert cfg["canary_remaining"] is None
        assert cfg["paused"] is True


def test_lift_canary_then_approve_still_refuses(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    from applypilot.apply import pgqueue
    with pgqueue.connect(fleet_db) as conn:
        hm.set_canary(conn, 3)
        hm.lift_canary(conn)
        try:
            hm.approve(conn, all_pushed=True)
            assert False, "approve must refuse after lift-canary disarms the gate"
        except SystemExit:
            pass


def test_push_home_invokes_push_inbox_outcomes(fleet_db, tmp_path, monkeypatch):
    """Phase 2.3: the home push cadence must also push email_events outcome
    summaries into PG inbox_outcomes -- today push_inbox_outcomes has zero
    production callers. Spy on sync.push_inbox_outcomes to prove push_home wires it in."""
    from applypilot.fleet import apply_home_main as hm
    from applypilot.fleet import sync
    from applypilot.apply import pgqueue

    calls = []
    real = sync.push_inbox_outcomes

    def _spy(*, sqlite_conn=None, pg_conn=None, limit=None):
        calls.append((sqlite_conn, pg_conn))
        return real(sqlite_conn=sqlite_conn, pg_conn=pg_conn, limit=limit)

    monkeypatch.setattr(sync, "push_inbox_outcomes", _spy)

    sq = sqlite3.connect(tmp_path / "home.db")
    sq.row_factory = sqlite3.Row
    sq.executescript(
        "CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT, "
        "apply_status TEXT, apply_error TEXT, audit_score REAL, fit_score REAL, full_description TEXT, "
        "liveness_status TEXT, duplicate_of_url TEXT, decision_source TEXT, "
        "fit_gap_category TEXT, recommended_action TEXT, audit_flags TEXT);"
        "CREATE TABLE email_events(message_id TEXT PRIMARY KEY, job_url TEXT, occurred_at TEXT, "
        "sender_domain TEXT, stage TEXT, outcome TEXT, title TEXT, company TEXT, confidence TEXT);"
    )
    sq.commit()

    with pgqueue.connect(fleet_db) as conn:
        hm.push_home(conn, sqlite_conn=sq, score_floor=7, limit=None)

    assert len(calls) == 1, "push_home must call sync.push_inbox_outcomes exactly once"


def test_push_home_survives_inbox_outcomes_failure(fleet_db, tmp_path, monkeypatch):
    """A transient/UndefinedTable failure in push_inbox_outcomes must be logged and
    swallowed, not crash the apply-queue staging push (Phase 2.3 best-effort contract)."""
    from applypilot.fleet import apply_home_main as hm
    from applypilot.fleet import sync
    from applypilot.apply import pgqueue

    def _boom(*, sqlite_conn=None, pg_conn=None, limit=None):
        raise RuntimeError("relation \"inbox_outcomes\" does not exist")

    monkeypatch.setattr(sync, "push_inbox_outcomes", _boom)

    sq = sqlite3.connect(tmp_path / "home.db")
    sq.row_factory = sqlite3.Row
    sq.executescript(
        "CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT, "
        "apply_status TEXT, apply_error TEXT, audit_score REAL, fit_score REAL, full_description TEXT, "
        "liveness_status TEXT, duplicate_of_url TEXT, decision_source TEXT, "
        "fit_gap_category TEXT, recommended_action TEXT, audit_flags TEXT);"
    )
    sq.commit()

    with pgqueue.connect(fleet_db) as conn:
        # Must not raise despite push_inbox_outcomes blowing up.
        n = hm.push_home(conn, sqlite_conn=sq, score_floor=7, limit=None)
    assert n == 0  # no eligible jobs staged; the point is it didn't raise


def test_push_home_threads_explicit_approved_batch(fleet_db, monkeypatch):
    from applypilot.fleet import apply_home_main as hm
    from applypilot.fleet import sync

    seen = {}

    def _push(**kwargs):
        seen["approved_batch"] = kwargs["approved_batch"]
        return 0

    monkeypatch.setattr(sync, "push_apply_eligible", _push)
    monkeypatch.setattr(sync, "push_inbox_outcomes", lambda **kwargs: 0)
    with pgqueue.connect(fleet_db) as conn:
        assert hm.push_home(conn, approved_batch="human-2026-07-12") == 0
    assert seen["approved_batch"] == "human-2026-07-12"


def test_apply_home_resolve_challenge(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    from applypilot.apply import pgqueue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, score, status, lane, apply_domain, lease_owner, "
            "lease_expires_at, apply_status, worker_id) "
            "VALUES ('p1','http://x','9','leased','ats','x.com','w1',"
            "now() + interval '3650 days','challenge_pending','w1')"
        )
        cur.execute("INSERT INTO auth_challenge (url, worker_id, kind, route) VALUES ('p1','w1','captcha','owner_inbox')")
        conn.commit()
        hm.resolve_challenge_cmd(conn, "p1", skip=False)
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM apply_queue WHERE url='p1'")
        assert cur.fetchone()["status"] == "queued"
        cur.execute("SELECT resolved_at FROM auth_challenge WHERE url='p1'")
        assert cur.fetchone()["resolved_at"] is not None
