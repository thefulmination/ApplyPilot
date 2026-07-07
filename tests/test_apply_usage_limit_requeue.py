"""Regression: an agent usage-limit / quota wall must RE-QUEUE the job, never park it
crash_unconfirmed.

Live incident (2026-06-29): a Codex-Spark "You've hit your usage limit -- try again at
8:10 PM -- Switch to another model" wall hit on the agent's FIRST turn (before any
browser/MCP tool call). run_job returned `failed:no_result_line`, which the fleet worker
maps to `crash_unconfirmed` (the "may have submitted, never re-lease" bucket). In minutes
~283 good, never-touched jobs were poisoned into a never-retried state.

A usage-limit failure with ZERO application-touching tool calls in the transcript
PROVABLY never touched the page -> it is safe to re-queue (status back to 'queued',
attempts not pinned). These tests pin that behavior AND the load-bearing safety gate:
a GENUINE mid-apply crash (any browser/form tool calls happened) must STILL be
classified no_result_line -> crash_unconfirmed.
"""
from __future__ import annotations

import io
import json

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
CLAUDE_SESSION_WALL = (
    "You've hit your session limit · resets 12:40pm (America/New_York)"
)


class _FakeStdin:
    def write(self, text):
        self.text = text

    def close(self):
        pass


class _FakeProc:
    pid = 12345
    returncode = 0

    def __init__(self, stdout_text):
        self.stdout = io.StringIO(stdout_text)
        self.stdin = _FakeStdin()

    def wait(self, timeout=None):
        return self.returncode


class _FakeWorkerState:
    actions = 0
    total_cost = 0.0
    last_action = ""


def _job():
    return {
        "title": "Analyst",
        "site": "Acme",
        "url": "https://example.com/job",
        "application_url": "https://example.com/apply",
        "fit_score": 8,
        "tailored_resume_path": None,
    }


def _patch_launcher_agent_io(monkeypatch, tmp_path, *, stdout_text="", popen_error=None):
    state = _FakeWorkerState()
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(launcher.config, "APP_DIR", tmp_path)
    monkeypatch.setattr(launcher.config, "LOG_DIR", log_dir)
    monkeypatch.setattr(launcher.config, "resolve_resume_stem", lambda path: None)
    monkeypatch.setattr(launcher, "_maybe_greenhouse_apply", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "_maybe_lever_shadow", lambda *a, **k: None)
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", lambda **kwargs: "prompt")
    monkeypatch.setattr(launcher, "build_apply_agent_command", lambda **kwargs: ["fake-agent"])
    monkeypatch.setattr(launcher, "add_event", lambda message: None)
    monkeypatch.setattr(launcher, "get_state", lambda worker_id: state)

    def fake_reset_worker_dir(worker_id):
        worker_dir = tmp_path / f"worker-{worker_id}"
        worker_dir.mkdir(parents=True, exist_ok=True)
        return worker_dir

    def fake_update_state(worker_id, **kwargs):
        for key, value in kwargs.items():
            setattr(state, key, value)

    def fake_popen(*args, **kwargs):
        if popen_error is not None:
            raise RuntimeError(popen_error)
        return _FakeProc(stdout_text)

    monkeypatch.setattr(launcher, "reset_worker_dir", fake_reset_worker_dir)
    monkeypatch.setattr(launcher, "update_state", fake_update_state)
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    return state


def _stream_line(payload):
    return json.dumps(payload) + "\n"


def test_usage_limit_transcript_with_no_tool_calls_is_retryable():
    """The exact incident: usage-limit wall, agent never called a browser tool."""
    assert launcher._no_result_status(CODEX_SPARK_WALL, tool_calls=0) == launcher.USAGE_LIMIT_STATUS
    assert launcher._no_result_status(CLAUDE_WALL, tool_calls=0) == launcher.USAGE_LIMIT_STATUS
    # and it is explicitly NOT the crash-bound no_result_line status
    assert launcher._no_result_status(CODEX_SPARK_WALL, tool_calls=0) != "failed:no_result_line"


def test_classified_usage_limit_stats_are_available_for_worker_metadata(monkeypatch):
    from applypilot.apply.failure_classification import FailureEvidence, classify_apply_failure

    result = classify_apply_failure(
        FailureEvidence(
            status="failed:no_result_line",
            transcript="You've hit your usage limit. Switch to another model.",
            application_tool_calls=0,
            tool_calls_total=0,
        )
    )

    assert result.failure_class == "usage_or_session_limit"
    assert result.safe_requeue is True


def test_launcher_metadata_counts_total_tool_calls_separately(monkeypatch, tmp_path):
    stdout_text = "".join(
        [
            _stream_line(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "ToolSearch", "input": {}},
                        ],
                    },
                }
            ),
            _stream_line(
                {
                    "type": "item.completed",
                    "item": {"type": "mcp_tool_call", "name": "browser_click"},
                }
            ),
            _stream_line(
                {
                    "type": "result",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "num_turns": 99,
                    "total_cost_usd": 0,
                    "result": "RESULT:FAILED:no_result_line",
                }
            ),
        ]
    )
    _patch_launcher_agent_io(monkeypatch, tmp_path, stdout_text=stdout_text)

    status, _duration_ms = launcher._run_job_impl(_job(), port=9222, worker_id=44)

    stats = launcher._last_run_stats[44]
    assert status == "failed:no_result_line"
    assert stats["tool_calls_total"] == 2
    assert stats["application_tool_calls"] == 1
    assert stats["last_tool"] == "browser_click"


def test_launcher_zero_tool_terminal_failure_keeps_last_tool_empty(monkeypatch, tmp_path):
    stdout_text = _stream_line(
        {
            "type": "result",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "num_turns": 1,
            "total_cost_usd": 0,
            "result": "RESULT:FAILED:no_result_line",
        }
    )
    _patch_launcher_agent_io(monkeypatch, tmp_path, stdout_text=stdout_text)

    status, _duration_ms = launcher._run_job_impl(_job(), port=9225, worker_id=47)

    stats = launcher._last_run_stats[47]
    assert status == "failed:no_result_line"
    assert stats["tool_calls_total"] == 0
    assert stats["application_tool_calls"] == 0
    assert stats["last_tool"] == ""


def test_launcher_exception_path_overwrites_stale_metadata(monkeypatch, tmp_path):
    _patch_launcher_agent_io(monkeypatch, tmp_path, popen_error="agent launch exploded")
    launcher._last_run_stats[45] = {
        "route": "stale",
        "failure_class": "stale",
        "tool_calls_total": 99,
        "application_tool_calls": 99,
    }

    status, _duration_ms = launcher._run_job_impl(_job(), port=9223, worker_id=45)

    stats = launcher._last_run_stats[45]
    assert status == "failed:agent launch exploded"
    assert stats["route"] == "agent"
    assert "agent launch exploded" in stats["transcript"]
    assert stats["failure_class"] == "malformed_result"
    assert stats["tool_calls_total"] == 0
    assert stats["application_tool_calls"] == 0


def test_greenhouse_owned_result_updates_launcher_metadata(monkeypatch):
    monkeypatch.setattr(launcher.config, "resolve_resume_stem", lambda path: None)
    monkeypatch.setattr(
        launcher,
        "_maybe_greenhouse_apply",
        lambda *a, **k: ("failed:no_confirmation", 42),
    )
    launcher._last_run_stats[46] = {"route": "stale", "failure_class": "stale"}

    status, duration_ms = launcher._run_job_impl(_job(), port=9224, worker_id=46)

    stats = launcher._last_run_stats[46]
    assert (status, duration_ms) == ("failed:no_confirmation", 42)
    assert stats["route"] == "adapter_submit:greenhouse"
    assert stats["last_tool"] == "greenhouse_adapter"
    assert stats["tool_calls_total"] == 0
    assert stats["application_tool_calls"] == 0
    assert stats["failure_class"] == "malformed_result"


def test_session_limit_wording_with_no_tool_calls_is_retryable():
    """Live incident 2026-07-03: Claude CLI switched to 'session limit' wording with a
    'resets 12:40pm (America/New_York)' reset format. The old regex only matched 'usage
    limit', so this wall went unclassified and a worker hung silently for 4 hours."""
    assert launcher._no_result_status(CLAUDE_SESSION_WALL, tool_calls=0) == launcher.USAGE_LIMIT_STATUS
    assert launcher._no_result_status(CLAUDE_SESSION_WALL, tool_calls=0) != "failed:no_result_line"


def test_usage_limit_signature_WITH_tool_calls_stays_no_result_line():
    """Safety gate: if any browser/form tool call happened the agent may have driven
    the form (a real mid-apply crash). It must STAY no_result_line ->
    crash_unconfirmed, NOT be re-queued -- even if the late transcript happens to
    mention a usage limit."""
    assert launcher._no_result_status(CODEX_SPARK_WALL, tool_calls=3) == "failed:no_result_line"
    assert launcher._no_result_status(CODEX_SPARK_WALL, tool_calls=1) == "failed:no_result_line"


def test_toolsearch_only_usage_wall_is_retryable():
    """Codex can emit ToolSearch before reporting a weekly/session wall. ToolSearch
    does not touch the application page, so it must not poison the job into
    crash_unconfirmed."""
    assert launcher._tool_call_touches_application("ToolSearch") is False
    assert launcher._tool_call_touches_application("tool_search") is False
    assert launcher._no_result_status(
        "  >> ToolSearch\nYou've hit your weekly limit · resets 3am (America/New_York)",
        tool_calls=0,
    ) == launcher.USAGE_LIMIT_STATUS


def test_browser_tool_still_counts_as_application_touch():
    assert launcher._tool_call_touches_application("browser_navigate") is True
    assert launcher._tool_call_touches_application("mcp_tool_call") is True


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
        "You've hit your session limit",
        "session limit reached",
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
