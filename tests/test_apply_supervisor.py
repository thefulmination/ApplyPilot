"""Smoke tests for the apply crash/stall supervisor's bound logic.

The supervisor is mostly subprocess orchestration (hard to unit-test end-to-end), so
these pin the SAFETY BOUNDS that must hold without ever spawning a real apply run:
it must stop on max_attempts and on an already-met budget, and never raise.
"""
from __future__ import annotations

from applypilot.apply import supervisor as S


def test_supervise_stops_on_max_attempts_without_spawning(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(S.config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(S, "_applied_count", lambda: 10)
    # If it tried to spawn, this would blow up -- it must NOT be reached.
    monkeypatch.setattr(S.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    S.supervise(total_cost_usd=100, max_attempts=0)
    out = capsys.readouterr().out
    assert "SUPERVISOR start" in out
    assert "max_attempts" in out
    assert (tmp_path / "supervisor.log").exists()


def test_supervise_stops_when_budget_already_met(tmp_path, monkeypatch):
    monkeypatch.setattr(S.config, "LOG_DIR", tmp_path)
    # baseline=10, then progress jumps to 200 -> est spend (190 * 1.5) >> budget -> stop,
    # without ever spawning a run.
    counts = iter([10, 200])
    monkeypatch.setattr(S, "_applied_count", lambda: next(counts, 200))
    monkeypatch.setattr(S, "_apply_cost_total", lambda: 0.0)  # no durable cost -> use estimate
    monkeypatch.setattr(S.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    S.supervise(total_cost_usd=5, est_cost_per_apply=1.5, max_attempts=5)
    assert (tmp_path / "supervisor.log").read_text(encoding="utf-8").count("budget") >= 1


def test_supervise_stops_on_actual_cost_even_with_few_applies(tmp_path, monkeypatch):
    # ACTUAL durable apply-agent spend drives the budget stop: only 1 new apply but $50
    # actually spent (failed/expired launches cost too) -> stop on real cost, not the
    # 1*$1.5 applied-count estimate. Never spawns.
    monkeypatch.setattr(S.config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(S, "_applied_count", lambda: 11)  # baseline+1
    costs = iter([0.0, 50.0])  # baseline snapshot, then current cumulative
    monkeypatch.setattr(S, "_apply_cost_total", lambda: next(costs, 50.0))
    monkeypatch.setattr(S.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    S.supervise(total_cost_usd=20, est_cost_per_apply=1.5, max_attempts=5)
    log = (tmp_path / "supervisor.log").read_text(encoding="utf-8")
    assert "budget" in log and "spent $50" in log


def test_apply_cost_total_sums_only_apply_agent_rows(tmp_path, monkeypatch):
    import sqlite3
    from applypilot import database
    db = tmp_path / "applypilot.db"
    database.init_db(db)
    c = sqlite3.connect(str(db))
    for stage, cost in [("apply_agent", 1.25), ("apply_agent", 0.75), ("scoring", 9.0)]:
        c.execute("INSERT INTO llm_usage (stage, est_cost_usd, created_at) VALUES (?, ?, 'now')",
                  (stage, cost))
    c.commit(); c.close()
    monkeypatch.setattr(S.config, "DB_PATH", db)
    assert abs(S._apply_cost_total() - 2.0) < 1e-9  # only the two apply_agent rows
