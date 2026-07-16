"""run_apply dynamic agent-switching: on a usage-limit wall the driver notes the wall,
switches the worker to the fallback agent, and (when all agents are walled) pauses until
the nearer reset instead of churning the queue. Fake loop + stub conn (no PG, no spend).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from applypilot.fleet import apply_worker_main as awm
from applypilot.fleet.agent_switch import AgentSwitcher

pytestmark = pytest.mark.usefixtures("acquisition_admitted")


class _StubCtx:
    def __enter__(self):
        return MagicMock()

    def __exit__(self, *a):
        return False


def _conn_factory():
    return _StubCtx()


def test_run_apply_switches_to_fallback_after_usage_limit(monkeypatch):
    awm._STOP_REQUESTED.clear()
    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: False)

    sw = AgentSwitcher("claude", "codex", cooldown_seconds=3600)
    rebuilt: list[str] = []

    def rebuild(agent):
        rebuilt.append(agent)
        return lambda job: {"run_status": "noop"}

    clock = {"t": 1000.0}
    actions = iter(["usage_limit", "applied"])
    loop = MagicMock()
    loop._log_tail_fn = None
    loop._current_model = "sonnet"
    agent_events = []

    def set_agent_telemetry(*, current_agent, current_model, agent_chain,
                            last_agent_switch_reason=None):
        agent_events.append({
            "current_agent": current_agent,
            "current_model": current_model,
            "agent_chain": agent_chain,
            "last_agent_switch_reason": last_agent_switch_reason,
        })

    loop.set_agent_telemetry = set_agent_telemetry

    def run_once():
        return {"action": next(actions), "url": "u"}

    loop.run_once = run_once

    counts = awm.run_apply(_conn_factory, loop, max_iterations=2, idle_sleep=0,
                           switcher=sw, rebuild_apply_fn=rebuild,
                           time_fn=lambda: clock["t"])

    assert rebuilt[0] == "claude"                 # tick 1 uses the preferred agent
    assert "codex" in rebuilt                     # tick 2 switched after the wall
    assert sw.blocked_until("claude") == 1000.0 + 3600   # walled for the cooldown
    assert counts["applied"] == 1
    assert agent_events == [
        {
            "current_agent": "claude",
            "current_model": "sonnet",
            "agent_chain": "claude>codex",
            "last_agent_switch_reason": "startup",
        },
        {
            "current_agent": "codex",
            "current_model": "sonnet",
            "agent_chain": "claude>codex",
            "last_agent_switch_reason": "switch:claude->codex",
        },
    ]
    awm._STOP_REQUESTED.clear()


def test_run_apply_updates_loop_agent_telemetry_on_switch(monkeypatch):
    from applypilot.fleet.agent_switch import AgentSwitcher
    from applypilot.fleet import apply_worker_main as M

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *args): return False

    class Loop:
        def __init__(self):
            self.apply_fn = None
            self._current_model = "sonnet"
            self.agent_events = []
            self.calls = 0

        def set_agent_telemetry(self, *, current_agent, current_model, agent_chain,
                                last_agent_switch_reason=None):
            self.agent_events.append({
                "current_agent": current_agent,
                "current_model": current_model,
                "agent_chain": agent_chain,
                "last_agent_switch_reason": last_agent_switch_reason,
            })

        def run_once(self):
            self.calls += 1
            return {"action": "idle"}

    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: False)
    monkeypatch.setattr(M, "_apply_timeout_override", lambda conn=None, dsn=None: None)

    loop = Loop()
    switcher = AgentSwitcher(agents=["claude", "codex"])
    rebuilt = []

    def rebuild(agent):
        rebuilt.append(agent)
        return lambda job: {"run_status": "failed:no_result_line", "agent": agent}

    M.run_apply(
        lambda: Conn(),
        loop,
        max_iterations=1,
        idle_sleep=0,
        switcher=switcher,
        rebuild_apply_fn=rebuild,
        time_fn=lambda: 1000.0,
    )

    assert rebuilt == ["claude"]
    assert loop.agent_events == [{
        "current_agent": "claude",
        "current_model": "sonnet",
        "agent_chain": "claude>codex",
        "last_agent_switch_reason": "startup",
    }]


def test_run_apply_pauses_when_all_agents_walled(monkeypatch):
    awm._STOP_REQUESTED.clear()
    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: False)

    sw = AgentSwitcher("claude", "codex", cooldown_seconds=3600)
    sw.note_wall("claude", now=1000.0)
    sw.note_wall("codex", now=1000.0)

    loop = MagicMock()
    loop._log_tail_fn = None

    counts = awm.run_apply(_conn_factory, loop, max_iterations=2, idle_sleep=0,
                           switcher=sw, rebuild_apply_fn=lambda a: (lambda job: {}),
                           time_fn=lambda: 1000.0)

    loop.run_once.assert_not_called()             # never leased while both are walled
    assert counts["idle"] == 2
    awm._STOP_REQUESTED.clear()


def test_run_apply_beats_while_all_agents_walled(monkeypatch):
    awm._STOP_REQUESTED.clear()
    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: False)

    sw = AgentSwitcher("claude", "codex", cooldown_seconds=3600)
    sw.note_wall("claude", now=1000.0)
    sw.note_wall("codex", now=1000.0)

    class LoopWithBeat:
        _log_tail_fn = None

        def __init__(self):
            self.beats = []
            self.ran = 0

        def _beat(self, conn, state):
            self.beats.append(state)

        def run_once(self):
            self.ran += 1
            return {"action": "applied"}

    loop = LoopWithBeat()

    counts = awm.run_apply(_conn_factory, loop, max_iterations=2, idle_sleep=0,
                           switcher=sw, rebuild_apply_fn=lambda a: (lambda job: {}),
                           time_fn=lambda: 1000.0)

    assert loop.beats == ["paused", "paused"]
    assert loop.ran == 0
    assert counts["idle"] == 2
    awm._STOP_REQUESTED.clear()


def test_run_apply_resumes_when_fleet_blocks_clear(monkeypatch):
    awm._STOP_REQUESTED.clear()
    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: False)

    sw = AgentSwitcher("claude", "codex", cooldown_seconds=3600)
    sw.note_wall("claude", now=1000.0)
    sw.note_wall("codex", now=1000.0)

    class Loop:
        _log_tail_fn = None

        def __init__(self):
            self.beats = []
            self.ran = 0

        def _beat(self, conn, state):
            self.beats.append(state)

        def run_once(self):
            self.ran += 1
            return {"action": "applied"}

    loop = Loop()
    budget = _FakeBudget()  # no blocks in DB -> should clear local stale walls
    counts = awm.run_apply(_conn_factory, loop, max_iterations=1, idle_sleep=0,
                           switcher=sw, rebuild_apply_fn=lambda a: (lambda j: {}),
                           time_fn=lambda: 10_000.0, budget=budget)

    assert loop.ran == 1
    assert sw.blocked_until("claude") == 0.0
    assert sw.blocked_until("codex") == 0.0
    assert counts["applied"] == 1
    awm._STOP_REQUESTED.clear()


def test_run_apply_handles_remote_command_before_all_agents_walled(monkeypatch):
    awm._STOP_REQUESTED.clear()
    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: False)

    sw = AgentSwitcher("claude", "codex", cooldown_seconds=3600)
    sw.note_wall("claude", now=1000.0)
    sw.note_wall("codex", now=1000.0)

    class LoopWithCommand:
        _log_tail_fn = None

        def __init__(self):
            self.handled = 0
            self.ran = 0

        def _handle_commands(self, conn):
            self.handled += 1
            return "restart"

        def run_once(self):
            self.ran += 1
            return {"action": "applied"}

    loop = LoopWithCommand()

    counts = awm.run_apply(_conn_factory, loop, max_iterations=1, idle_sleep=0,
                           switcher=sw, rebuild_apply_fn=lambda a: (lambda job: {}),
                           time_fn=lambda: 1000.0)

    assert loop.handled == 1
    assert loop.ran == 0
    assert counts == {"applied": 0, "halted": 0, "idle": 0, "error": 0}
    awm._STOP_REQUESTED.clear()


def test_run_apply_uses_parsed_reset_time_over_cooldown(monkeypatch):
    awm._STOP_REQUESTED.clear()
    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: False)

    sw = AgentSwitcher("claude", "codex", cooldown_seconds=3600)
    now_epoch = datetime(2026, 7, 3, 11, 23, tzinfo=timezone.utc).timestamp()
    reset_dt = datetime(2026, 7, 3, 12, 40, tzinfo=timezone.utc)

    loop = MagicMock()
    loop._log_tail_fn = lambda: "You've hit your session limit · resets 12:40pm"
    loop.run_once = lambda: {"action": "usage_limit", "url": "u"}

    awm.run_apply(_conn_factory, loop, max_iterations=1, idle_sleep=0,
                  switcher=sw, rebuild_apply_fn=lambda a: (lambda job: {}),
                  time_fn=lambda: now_epoch,
                  now_local_fn=lambda: datetime(2026, 7, 3, 11, 23, tzinfo=timezone.utc))

    # blocked until the PARSED 12:40pm reset, not now+3600 (which would be ~12:23)
    assert sw.blocked_until("claude") == reset_dt.timestamp()
    awm._STOP_REQUESTED.clear()


def test_run_apply_records_short_block_for_just_passed_try_again(monkeypatch):
    awm._STOP_REQUESTED.clear()
    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: False)

    sw = AgentSwitcher("codex", "claude", cooldown_seconds=3600)
    now_dt = datetime(2026, 7, 5, 17, 10, 26, tzinfo=timezone.utc)
    expected = datetime(2026, 7, 5, 17, 12, 26, tzinfo=timezone.utc).timestamp()
    budget = _FakeBudget()

    loop = MagicMock()
    loop._log_tail_fn = lambda: (
        "You've hit your usage limit. Visit [REDACTED] to purchase more credits "
        "or try again at 5:10 PM."
    )
    loop.run_once = lambda: {"action": "usage_limit", "url": "u"}

    awm.run_apply(_conn_factory, loop, max_iterations=1, idle_sleep=0,
                  switcher=sw, rebuild_apply_fn=lambda a: (lambda job: {}),
                  time_fn=lambda: now_dt.timestamp(), budget=budget,
                  now_local_fn=lambda: now_dt)

    assert sw.blocked_until("codex") == expected
    assert budget.recorded == [("codex", expected)]
    awm._STOP_REQUESTED.clear()


class _FakeBudget:
    """Stand-in for the PG-backed fleet budget: scripted shared blocks, records walls."""
    def __init__(self, blocks=None):
        self._blocks = blocks or {}
        self.recorded = []
        self.evaluated = 0

    def blocks(self, conn):
        return dict(self._blocks)

    def record_wall(self, conn, agent, blocked_until_epoch):
        self.recorded.append((agent, blocked_until_epoch))

    def maybe_evaluate(self, conn):
        self.evaluated += 1


def test_run_apply_respects_fleet_wide_block(monkeypatch):
    # A block set by ANOTHER worker (or the predictive monitor) shifts THIS worker off the
    # blocked agent even though it never walled locally.
    awm._STOP_REQUESTED.clear()
    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: False)

    sw = AgentSwitcher(agents=["claude", "codex"], cooldown_seconds=3600)
    budget = _FakeBudget(blocks={"claude": 9_999_999_999.0})   # claude blocked far in the future
    rebuilt: list[str] = []
    loop = MagicMock()
    loop._log_tail_fn = None
    loop.run_once = lambda: {"action": "applied"}

    awm.run_apply(_conn_factory, loop, max_iterations=1, idle_sleep=0,
                  switcher=sw, rebuild_apply_fn=lambda a: (rebuilt.append(a) or (lambda j: {})),
                  model_for_agent=lambda a: f"{a}-model",
                  time_fn=lambda: 1000.0, budget=budget)

    assert rebuilt == ["codex"]        # fleet-blocked claude -> codex, without a local wall
    assert budget.evaluated >= 1       # ran the predictive evaluator
    assert loop.current_agent == "codex"
    assert loop.current_model == "codex-model"
    assert loop.agent_chain == "claude,codex"
    awm._STOP_REQUESTED.clear()


def test_run_apply_propagates_local_wall_to_budget(monkeypatch):
    awm._STOP_REQUESTED.clear()
    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: False)

    sw = AgentSwitcher(agents=["claude", "codex"], cooldown_seconds=3600)
    budget = _FakeBudget()
    loop = MagicMock()
    loop._log_tail_fn = None
    loop.run_once = lambda: {"action": "usage_limit", "url": "u"}

    awm.run_apply(_conn_factory, loop, max_iterations=1, idle_sleep=0,
                  switcher=sw, rebuild_apply_fn=lambda a: (lambda j: {}),
                  time_fn=lambda: 1000.0, budget=budget)

    # local claude wall -> recorded fleet-wide at now + cooldown so other workers skip it too
    assert budget.recorded == [("claude", 1000.0 + 3600)]
    awm._STOP_REQUESTED.clear()


def test_pg_agent_budget_throttles_evaluate(monkeypatch):
    calls = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, statement, params): calls.append((statement, params))
        def fetchone(self): return {"blocks": {}}

    class Connection:
        def cursor(self): return Cursor()
        def commit(self): return None

    clock = {"t": 0.0}
    b = awm.PgAgentBudget(soft_caps={"claude": 5.0}, eval_interval_seconds=100,
                          time_fn=lambda: clock["t"])
    b.maybe_evaluate(Connection())
    assert len(calls) == 1        # first call runs
    assert "fleet_worker_evaluate_agent_budget" in calls[0][0]
    clock["t"] = 50
    b.maybe_evaluate(Connection())
    assert len(calls) == 1        # throttled
    clock["t"] = 101
    b.maybe_evaluate(Connection())
    assert len(calls) == 2        # due again


def test_pg_agent_budget_no_caps_skips_evaluate(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr("applypilot.fleet.agent_budget.evaluate_soft_blocks",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), [])[1])
    b = awm.PgAgentBudget(soft_caps={}, time_fn=lambda: 0.0)   # no caps -> predictive off
    b.maybe_evaluate(object())
    assert calls["n"] == 0


def test_run_apply_without_switcher_is_unchanged(monkeypatch):
    # Back-compat: no switcher -> the driver behaves exactly as before.
    awm._STOP_REQUESTED.clear()
    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: False)
    loop = MagicMock()
    loop.run_once = lambda: {"action": "applied"}
    counts = awm.run_apply(_conn_factory, loop, max_iterations=1, idle_sleep=0)
    assert counts["applied"] == 1
    awm._STOP_REQUESTED.clear()


def test_build_apply_loop_reports_worker_version(monkeypatch):
    monkeypatch.setattr(awm, "_setup_apply_env", lambda: None)
    monkeypatch.setattr(awm, "_apply_timeout_override", lambda dsn=None, conn=None: None)
    monkeypatch.setattr(awm, "make_apply_fn", lambda model, agent, slot, **_kwargs: lambda job: {})
    monkeypatch.setattr(awm, "make_log_tail_fn", lambda slot: lambda: None)
    monkeypatch.setattr(awm, "worker_version", lambda: "0.3.0+git.test.abc123")

    loop = awm.build_apply_loop(
        dsn="postgres://unused",
        worker_id="m4-0",
        home_ip="100.64.0.1",
        model="sonnet",
        agent="codex",
        machine_owner="m4",
        slot=0,
    )

    assert loop.sw_version == "0.3.0+git.test.abc123"
    assert loop.current_agent == "codex"
    assert loop.current_model == "codex-default"
