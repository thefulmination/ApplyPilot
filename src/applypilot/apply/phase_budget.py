"""Per-phase time, turn, and dollar budget enforcement."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
import time


@dataclass(frozen=True)
class PhaseLimit:
    max_seconds: float
    max_turns: int
    max_cost_usd: float


@dataclass
class PhaseUsage:
    elapsed_seconds: float = 0.0
    turns: int = 0
    cost_usd: float = 0.0


class PhaseBudgetExceeded(RuntimeError):
    def __init__(self, phase: str, dimension: str) -> None:
        self.phase = phase
        self.dimension = dimension
        super().__init__(f"{phase}:{dimension}")


def default_limits() -> dict[str, PhaseLimit]:
    defaults = {
        "preflight": PhaseLimit(30, 0, 0),
        "authentication": PhaseLimit(300, 0, 0),
        "form_fill": PhaseLimit(300, 0, 0),
        "answer": PhaseLimit(30, 1, 0.05),
        "recovery": PhaseLimit(60, 0, 0),
        "confirmation": PhaseLimit(45, 0, 0),
    }
    out = {}
    for phase, limit in defaults.items():
        prefix = f"APPLYPILOT_BUDGET_{phase.upper()}"
        out[phase] = PhaseLimit(
            float(os.environ.get(f"{prefix}_SECONDS") or limit.max_seconds),
            int(os.environ.get(f"{prefix}_TURNS") or limit.max_turns),
            float(os.environ.get(f"{prefix}_USD") or limit.max_cost_usd),
        )
    return out


class PhaseBudgetManager:
    def __init__(self, limits: dict[str, PhaseLimit] | None = None, *, time_fn=None) -> None:
        self.limits = limits or default_limits()
        self.usage = {phase: PhaseUsage() for phase in self.limits}
        self._time = time_fn or time.monotonic

    def _require_phase(self, phase: str) -> tuple[PhaseLimit, PhaseUsage]:
        if phase not in self.limits:
            raise ValueError(f"unknown budget phase {phase!r}")
        return self.limits[phase], self.usage[phase]

    def check(self, phase: str) -> None:
        limit, usage = self._require_phase(phase)
        if usage.elapsed_seconds > limit.max_seconds:
            raise PhaseBudgetExceeded(phase, "time")
        if usage.turns > limit.max_turns:
            raise PhaseBudgetExceeded(phase, "turns")
        if usage.cost_usd > limit.max_cost_usd + 1e-9:
            raise PhaseBudgetExceeded(phase, "cost")

    def reserve(self, phase: str, *, turns: int = 0, cost_usd: float = 0) -> None:
        _limit, usage = self._require_phase(phase)
        usage.turns += int(turns)
        usage.cost_usd += float(cost_usd)
        try:
            self.check(phase)
        except PhaseBudgetExceeded:
            usage.turns -= int(turns)
            usage.cost_usd -= float(cost_usd)
            raise

    @contextmanager
    def track(self, phase: str):
        self.check(phase)
        started = self._time()
        try:
            yield
        finally:
            _limit, usage = self._require_phase(phase)
            usage.elapsed_seconds += max(0.0, self._time() - started)
            self.check(phase)

    def metadata(self) -> dict:
        return {
            phase: {
                "elapsed_seconds": round(usage.elapsed_seconds, 6),
                "turns": usage.turns,
                "cost_usd": round(usage.cost_usd, 6),
                "limit_seconds": self.limits[phase].max_seconds,
                "limit_turns": self.limits[phase].max_turns,
                "limit_cost_usd": self.limits[phase].max_cost_usd,
            }
            for phase, usage in self.usage.items()
        }
