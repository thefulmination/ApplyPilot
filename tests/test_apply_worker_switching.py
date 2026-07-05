"""run_apply dynamic agent-switching: on a usage-limit wall the driver notes the wall,
switches the worker to the fallback agent, and (when all agents are walled) pauses until
the nearer reset instead of churning the queue. Fake loop + stub conn (no PG, no spend).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from applypilot.fleet import apply_worker_main as awm
from applypilot.fleet.agent_switch import AgentSwitcher


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
    awm._STOP_REQUESTED.clear()


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
                  time_fn=lambda: 1000.0, budget=budget)

    assert rebuilt == ["codex"]        # fleet-blocked claude -> codex, without a local wall
    assert budget.evaluated >= 1       # ran the predictive evaluator
    assert loop.current_agent == "codex"
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
    calls = {"n": 0}
    monkeypatch.setattr("applypilot.fleet.agent_budget.evaluate_soft_blocks",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), [])[1])
    clock = {"t": 0.0}
    b = awm.PgAgentBudget(soft_caps={"claude": 5.0}, eval_interval_seconds=100,
                          time_fn=lambda: clock["t"])
    b.maybe_evaluate(object())
    assert calls["n"] == 1        # first call runs
    clock["t"] = 50
    b.maybe_evaluate(object())
    assert calls["n"] == 1        # throttled
    clock["t"] = 101
    b.maybe_evaluate(object())
    assert calls["n"] == 2        # due again


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
