from __future__ import annotations

import pytest

from applypilot.apply.phase_budget import (
    PhaseBudgetExceeded,
    PhaseBudgetManager,
    PhaseLimit,
)
from applypilot.apply.workday_adapter import WorkdayAdapterRunner


def _limits(**overrides):
    base = {
        phase: PhaseLimit(100, 10, 10)
        for phase in ("preflight", "authentication", "form_fill", "answer", "recovery", "confirmation")
    }
    base.update(overrides)
    return base


def test_turn_and_cost_reservations_are_atomic():
    budget = PhaseBudgetManager(_limits(answer=PhaseLimit(30, 1, 0.05)))
    budget.reserve("answer", turns=1, cost_usd=0.02)
    with pytest.raises(PhaseBudgetExceeded, match="answer:turns"):
        budget.reserve("answer", turns=1, cost_usd=0.01)
    usage = budget.metadata()["answer"]
    assert usage["turns"] == 1
    assert usage["cost_usd"] == 0.02


def test_track_enforces_elapsed_time_per_phase():
    ticks = iter((0.0, 1.5))
    budget = PhaseBudgetManager(
        _limits(confirmation=PhaseLimit(1.0, 0, 0)),
        time_fn=lambda: next(ticks),
    )
    with pytest.raises(PhaseBudgetExceeded, match="confirmation:time"):
        with budget.track("confirmation"):
            pass
    assert budget.metadata()["confirmation"]["elapsed_seconds"] == 1.5


def test_unknown_phase_is_rejected():
    budget = PhaseBudgetManager(_limits())
    with pytest.raises(ValueError, match="unknown budget phase"):
        budget.check("everything")


class SnapshotOnlyDriver:
    def snapshot(self):
        return {"automation_ids": ["reviewPage"]}


def test_workday_runner_parks_when_form_time_budget_is_exhausted():
    ticks = iter((0.0, 0.1))
    budget = PhaseBudgetManager(
        _limits(form_fill=PhaseLimit(0.0, 0, 0)),
        time_fn=lambda: next(ticks),
    )
    result = WorkdayAdapterRunner(
        SnapshotOnlyDriver(),
        profile={},
        budget=budget,
    ).execute(submit=False)
    assert result.status == "parked"
    assert result.reason == "budget_exhausted:form_fill:time"
    assert result.metadata["phase_budget"]["form_fill"]["elapsed_seconds"] == 0.1
