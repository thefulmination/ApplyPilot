"""Regression: an agent usage-limit / quota wall must RE-QUEUE the job, never park it
crash_unconfirmed.

Live incident (2026-06-29): a Codex-Spark "You've hit your usage limit -- try again at
8:10 PM -- Switch to another model" wall hit on the agent's FIRST turn (before any
browser/MCP tool call). run_job returned `failed:no_result_line`, which the fleet worker
maps to `crash_unconfirmed` (the "may have submitted, never re-lease" bucket). In minutes
~283 good, never-touched jobs were poisoned into a never-retried state.

A usage-limit failure with ZERO tool calls in the transcript PROVABLY never touched the
page -> it is safe to re-queue (status back to 'queued', attempts not pinned). These tests
pin that behavior AND the load-bearing safety gate: a GENUINE mid-apply crash (any tool
calls happened) must STILL be classified no_result_line -> crash_unconfirmed.
"""
from __future__ import annotations

from applypilot.apply import launcher
from applypilot.apply import container_worker
from applypilot.apply import pgqueue


# ---------------------------------------------------------------------------
# 1. launcher classification: usage-limit transcript (0 tool calls) -> retryable
# ---------------------------------------------------------------------------

CODEX_SPARK_WALL = (
    "stream error: You've hit your usage limit. Try again at 8:10 PM. "
    "Switch to another model to keep working."
)
CLAUDE_WALL = (
    "Claude usage limit reached. Your limit will reset at 8pm. "
    "Upgrade to continue, or try again later."
)


def test_usage_limit_transcript_with_no_tool_calls_is_retryable():
    """The exact incident: usage-limit wall, agent never called a browser tool."""
    assert launcher._no_result_status(CODEX_SPARK_WALL, tool_calls=0) == launcher.USAGE_LIMIT_STATUS
    assert launcher._no_result_status(CLAUDE_WALL, tool_calls=0) == launcher.USAGE_LIMIT_STATUS
    # and it is explicitly NOT the crash-bound no_result_line status
    assert launcher._no_result_status(CODEX_SPARK_WALL, tool_calls=0) != "failed:no_result_line"


def test_usage_limit_signature_WITH_tool_calls_stays_no_result_line():
    """Safety gate: if ANY tool call happened the agent may have driven the form (a real
    mid-apply crash). It must STAY no_result_line -> crash_unconfirmed, NOT be re-queued --
    even if the late transcript happens to mention a usage limit."""
    assert launcher._no_result_status(CODEX_SPARK_WALL, tool_calls=3) == "failed:no_result_line"
    assert launcher._no_result_status(CODEX_SPARK_WALL, tool_calls=1) == "failed:no_result_line"


def test_plain_no_result_transcript_stays_no_result_line():
    """No usage-limit signature -> the existing no_result_line classification is unchanged."""
    assert launcher._no_result_status("", tool_calls=0) == "failed:no_result_line"
    assert launcher._no_result_status("some unrelated agent chatter\n>> navigate", tool_calls=0) \
        == "failed:no_result_line"


def test_is_usage_limit_signature_phrases():
    for txt in (
        "You've hit your usage limit",
        "usage limit reached",
        "Switch to another model",
        "try again at 8:10 PM",
        "quota exceeded for this account",
    ):
        assert launcher._is_usage_limit_signature(txt) is True
    for txt in ("application submitted", "captcha challenge", "role no longer accepting", ""):
        assert launcher._is_usage_limit_signature(txt) is False


def test_is_usage_limit_result_helper():
    assert launcher.is_usage_limit_result("failed:usage_limit") is True
    assert launcher.is_usage_limit_result(launcher.USAGE_LIMIT_STATUS) is True
    assert launcher.is_usage_limit_result("failed:no_result_line") is False
    assert launcher.is_usage_limit_result("applied") is False
    assert launcher.is_usage_limit_result(None) is False


def test_usage_limit_is_not_a_permanent_failure():
    """Home supervised path: a usage_limit must stay RETRYABLE (attempts not pinned)."""
    assert launcher._is_permanent_failure("failed:usage_limit") is False


def test_usage_limit_is_systemic_so_a_storm_halts_the_supervised_run():
    """A usage-limit storm should trip the global breaker (halt + keep jobs retryable),
    exactly like a no_result_line/timeout outage -- not burn through the whole queue."""
    assert launcher._is_systemic_failure("failed:usage_limit") is True


# ---------------------------------------------------------------------------
# 2. container_worker routing: usage_limit -> requeue; no_result_line -> crash
# ---------------------------------------------------------------------------

def test_container_worker_routes_usage_limit_to_requeue(monkeypatch):
    calls: dict[str, dict] = {}
    monkeypatch.setattr(pgqueue, "requeue_job",
                        lambda *a, **k: calls.setdefault("requeue", {"a": a, "k": k}) or True)
    monkeypatch.setattr(pgqueue, "write_result",
                        lambda *a, **k: calls.setdefault("write", {"a": a, "k": k}) or True)

    action = container_worker._handle_run_status(
        pg=None, worker_id=0, url="u1", status="failed:usage_limit",
        cost=0.0, dur_ms=0, model="deepseek-chat")

    assert action == "requeued"
    assert "requeue" in calls
    assert "write" not in calls  # never written as a terminal/crash row


def test_container_worker_no_result_line_still_crash_unconfirmed(monkeypatch):
    """Don't relax crash_unconfirmed for genuine mid-apply crashes."""
    calls: dict[str, dict] = {}
    monkeypatch.setattr(pgqueue, "requeue_job",
                        lambda *a, **k: calls.setdefault("requeue", {"a": a, "k": k}) or True)
    monkeypatch.setattr(pgqueue, "write_result",
                        lambda *a, **k: calls.setdefault("write", {"a": a, "k": k}) or True)

    action = container_worker._handle_run_status(
        pg=None, worker_id=0, url="u2", status="failed:no_result_line",
        cost=0.0, dur_ms=0, model="deepseek-chat")

    assert action == "other"
    assert "requeue" not in calls
    assert calls["write"]["k"]["status"] == "crash_unconfirmed"


def test_map_status_no_result_line_unchanged():
    assert container_worker._map_status("failed:no_result_line")[0] == "crash_unconfirmed"
    assert container_worker._map_status("failed:timeout")[0] == "crash_unconfirmed"
    assert container_worker._map_status("failed:worker_error:X")[0] == "crash_unconfirmed"


# ---------------------------------------------------------------------------
# 3. pgqueue.requeue_job SQL semantics (needs the disposable test Postgres)
# ---------------------------------------------------------------------------

def _insert_leased(conn, url, *, owner, attempts):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lease_owner, "
            "lease_expires_at, attempts, apply_domain) "
            "VALUES (%s, 'http://acme.com/x', 9, 'leased', %s, now() + interval '600 sec', %s, 'acme.com')",
            (url, owner, attempts),
        )
    conn.commit()


def test_requeue_job_sets_queued_and_does_not_pin_attempts(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _insert_leased(conn, "u/wall", owner="0", attempts=1)
        landed = pgqueue.requeue_job(conn, "0", "u/wall", apply_error="usage_limit")
        assert landed is True
        with conn.cursor() as cur:
            cur.execute("SELECT status, attempts, lease_owner, lease_expires_at "
                        "FROM apply_queue WHERE url='u/wall'")
            r = cur.fetchone()
    assert r["status"] == "queued"          # re-leasable, NOT crash_unconfirmed
    assert r["attempts"] != 99              # attempts NOT pinned
    assert r["attempts"] <= 1               # lease bump undone (never touched the page)
    assert r["lease_owner"] is None
    assert r["lease_expires_at"] is None


def test_requeue_job_is_lease_owner_guarded(fleet_db):
    """Only the lease holder may re-queue (mirrors write_result's guard)."""
    with pgqueue.connect(fleet_db) as conn:
        _insert_leased(conn, "u/other", owner="0", attempts=1)
        landed = pgqueue.requeue_job(conn, "99", "u/other", apply_error="usage_limit")
        assert landed is False
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM apply_queue WHERE url='u/other'")
            assert cur.fetchone()["status"] == "leased"   # untouched
