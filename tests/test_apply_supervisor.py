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
    monkeypatch.setattr(S.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    S.supervise(total_cost_usd=5, est_cost_per_apply=1.5, max_attempts=5)
    assert (tmp_path / "supervisor.log").read_text(encoding="utf-8").count("budget") >= 1
