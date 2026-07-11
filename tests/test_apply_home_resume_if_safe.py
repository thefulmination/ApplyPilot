"""Task 3: guarded self-resume (`apply-home resume-if-safe`).

resume_if_safe(conn) clears ONLY a plain `paused` flag so the autonomous
ApplyCycle can self-resume after a cap window frees capacity. SAFETY-CRITICAL:
it must NEVER override a Doctor/LinkedIn safety pause (ats_paused), and must
never resume into an exceeded cost cap.
"""
from applypilot.apply import pgqueue


def _get_flags(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT paused, ats_paused FROM fleet_config WHERE id=1")
        row = cur.fetchone()
    return row["paused"], row["ats_paused"]


def test_resume_if_safe_clears_plain_pause(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET paused=TRUE, ats_paused=FALSE WHERE id=1")
        conn.commit()
    with pgqueue.connect(fleet_db) as conn:
        result = hm.resume_if_safe(conn)
        assert result is True
        paused, ats_paused = _get_flags(conn)
        assert paused is False
        assert ats_paused is False


def test_resume_if_safe_never_overrides_ats_paused(fleet_db):
    """THE CATASTROPHE GUARD: a Doctor safety pause (ats_paused=TRUE) must never
    be cleared by resume_if_safe, and the plain `paused` flag must be left
    untouched too -- this path must be a complete no-op when ats_paused is set."""
    from applypilot.fleet import apply_home_main as hm
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET paused=TRUE, ats_paused=TRUE, "
                     "ats_pause_source='doctor' WHERE id=1")
        conn.commit()
    with pgqueue.connect(fleet_db) as conn:
        result = hm.resume_if_safe(conn)
        assert result is False
        paused, ats_paused = _get_flags(conn)
        assert paused is True, "paused must remain unchanged (still TRUE) when ats_paused blocks resume"
        assert ats_paused is True, "ats_paused must never be touched by resume_if_safe"
        with conn.cursor() as cur:
            cur.execute("SELECT ats_pause_source FROM fleet_config WHERE id=1")
            assert cur.fetchone()["ats_pause_source"] == "doctor", "ats_pause_source must never be touched"


def test_resume_if_safe_clears_stale_codex_canary_pause_without_live_blockers(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET paused=TRUE, ats_paused=TRUE, "
                    "ats_pause_source='codex_canary_blocked_agent_limits' WHERE id=1")
        conn.commit()
    with pgqueue.connect(fleet_db) as conn:
        result = hm.resume_if_safe(conn)
        assert result is True
        paused, ats_paused = _get_flags(conn)
        assert paused is False
        assert ats_paused is False
        with conn.cursor() as cur:
            cur.execute("SELECT ats_pause_source FROM fleet_config WHERE id=1")
            assert cur.fetchone()["ats_pause_source"] is None


def test_resume_if_safe_keeps_codex_canary_pause_when_paused_breaker_is_live(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET paused=TRUE, ats_paused=TRUE, "
                    "ats_pause_source='codex_canary_blocked_agent_limits' WHERE id=1")
        cur.execute("INSERT INTO rate_governor (scope_key, breaker_state) VALUES (%s, %s)",
                    ("host:example.com", "paused"))
        conn.commit()
    with pgqueue.connect(fleet_db) as conn:
        result = hm.resume_if_safe(conn)
        assert result is False
        paused, ats_paused = _get_flags(conn)
        assert paused is True
        assert ats_paused is True
        with conn.cursor() as cur:
            cur.execute("SELECT ats_pause_source FROM fleet_config WHERE id=1")
            assert cur.fetchone()["ats_pause_source"] == "codex_canary_blocked_agent_limits"


def test_resume_if_safe_keeps_codex_canary_pause_when_active_knob_exists(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET paused=TRUE, ats_paused=TRUE, "
                    "ats_pause_source='codex_canary_blocked_agent_limits' WHERE id=1")
        cur.execute(
            "INSERT INTO fleet_knobs (knob_type, scope_key, value_text, reason, created_by, active) "
            "VALUES (%s, %s, %s, %s, %s, TRUE)",
            ("pace_or_pause", "ats", "paused", "test live blocker", "doctor"),
        )
        conn.commit()
    with pgqueue.connect(fleet_db) as conn:
        result = hm.resume_if_safe(conn)
        assert result is False
        paused, ats_paused = _get_flags(conn)
        assert paused is True
        assert ats_paused is True
        with conn.cursor() as cur:
            cur.execute("SELECT ats_pause_source FROM fleet_config WHERE id=1")
            assert cur.fetchone()["ats_pause_source"] == "codex_canary_blocked_agent_limits"


def test_resume_if_safe_leaves_paused_when_cap_exceeded(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET paused=TRUE, ats_paused=FALSE, "
                     "cost_cap_daily_usd=1 WHERE id=1")
        cur.execute("INSERT INTO llm_usage (cost_usd, ts) VALUES (5, now())")
        conn.commit()
    with pgqueue.connect(fleet_db) as conn:
        result = hm.resume_if_safe(conn)
        assert result is False
        paused, _ = _get_flags(conn)
        assert paused is True, "paused must remain unchanged when cost cap is exceeded"


def test_resume_if_safe_noop_when_already_running(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET paused=FALSE, ats_paused=FALSE WHERE id=1")
        conn.commit()
    with pgqueue.connect(fleet_db) as conn:
        result = hm.resume_if_safe(conn)
        assert result is False
        paused, _ = _get_flags(conn)
        assert paused is False
