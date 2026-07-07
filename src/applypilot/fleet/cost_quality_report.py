"""Pure cost-quality aggregation for fleet apply reporting.

This module intentionally accepts already-loaded row dictionaries and performs
no database reads or writes. Callers decide where rows come from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping


TERMINAL_STATUSES = {"applied", "failed", "blocked", "crash_unconfirmed"}


@dataclass
class CountCost:
    count: int = 0
    applied: int = 0
    terminal: int = 0
    cost: float = 0.0

    @property
    def success_pct(self) -> float:
        if not self.count:
            return 0.0
        return (self.applied / self.count) * 100.0

    @property
    def cost_per_applied(self) -> float:
        if not self.applied:
            return 0.0
        return self.cost / self.applied


@dataclass
class FailureBucket:
    count: int = 0
    cost: float = 0.0


@dataclass
class FleetQueueSummary:
    applied: int = 0
    terminal_attempts: int = 0
    queued_or_leased: int = 0
    total_cost_usd: float = 0.0
    cost_per_applied_all_in: float = 0.0
    cost_per_terminal_attempt: float = 0.0
    by_ats: dict[str, CountCost] = field(default_factory=dict)
    by_failure_bucket: dict[str, FailureBucket] = field(default_factory=dict)


@dataclass
class LocalJobsSummary:
    touched: int = 0
    applied: int = 0
    by_ats: dict[str, CountCost] = field(default_factory=dict)
    by_failure_bucket: dict[str, FailureBucket] = field(default_factory=dict)


@dataclass
class CostQualityReport:
    fleet: FleetQueueSummary
    local: LocalJobsSummary


def _money(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def classify_ats(url: str | None) -> str:
    token = (url or "").lower()
    if "ashbyhq.com" in token:
        return "ashby"
    if "greenhouse" in token or "grnh.se" in token:
        return "greenhouse"
    if "lever.co" in token:
        return "lever"
    if "workdayjobs.com" in token or "myworkdayjobs.com" in token:
        return "workday"
    if "smartrecruiters" in token:
        return "smartrecruiters"
    if "workable.com" in token:
        return "workable"
    return "other"


def classify_failure_bucket(status: str | None, apply_error: str | None) -> str:
    status_token = (status or "").strip().lower()
    error_token = (apply_error or "").strip().lower()

    if _contains_any(error_token, ("email", "otp", "auth", "login", "verification")):
        return "email_auth_related"
    if _contains_any(error_token, ("captcha", "challenge")):
        return "challenge_related"
    if status_token == "crash_unconfirmed" or _contains_any(
        error_token, ("browser", "no_result", "timeout")
    ):
        return "agent_browser_runtime"
    if _contains_any(
        error_token,
        ("expired", "not_eligible", "location", "already_applied", "excluded"),
    ):
        return "preflight_or_policy"
    return "other"


def summarize_fleet_queue(rows: Iterable[dict]) -> FleetQueueSummary:
    by_ats: dict[str, CountCost] = {}
    by_failure_bucket: dict[str, FailureBucket] = {}
    applied = 0
    terminal_attempts = 0
    queued_or_leased = 0
    total_cost = 0.0

    for row in rows:
        status = _status(_get(row, "status", "apply_status"))
        cost = _money(_get(row, "cost_usd", "est_cost_usd", "apply_cost_usd"))
        ats = classify_ats(_get(row, "application_url", "url"))
        apply_error = _get(row, "apply_error", "error")

        is_applied = status == "applied"
        is_terminal = status in TERMINAL_STATUSES
        applied += int(is_applied)
        terminal_attempts += int(is_terminal)
        queued_or_leased += int(not is_terminal)
        total_cost += cost

        _add_count_cost(by_ats, ats, cost=cost, is_applied=is_applied, is_terminal=is_terminal)
        if is_terminal and not is_applied:
            bucket = classify_failure_bucket(status, apply_error)
            _add_failure_bucket(by_failure_bucket, bucket, cost=cost)

    return FleetQueueSummary(
        applied=applied,
        terminal_attempts=terminal_attempts,
        queued_or_leased=queued_or_leased,
        total_cost_usd=total_cost,
        cost_per_applied_all_in=_divide(total_cost, applied),
        cost_per_terminal_attempt=_divide(total_cost, terminal_attempts),
        by_ats=dict(sorted(by_ats.items())),
        by_failure_bucket=dict(sorted(by_failure_bucket.items())),
    )


def summarize_local_jobs(rows: Iterable[dict]) -> LocalJobsSummary:
    by_ats: dict[str, CountCost] = {}
    by_failure_bucket: dict[str, FailureBucket] = {}
    touched = 0
    applied = 0

    for row in rows:
        status = _status(_get(row, "apply_status", "status"))
        cost = _money(_get(row, "cost_usd", "est_cost_usd", "apply_cost_usd"))
        ats = classify_ats(_get(row, "application_url", "url"))
        apply_error = _get(row, "apply_error", "error")

        is_applied = status == "applied"
        is_terminal = status in TERMINAL_STATUSES
        touched += 1
        applied += int(is_applied)

        _add_count_cost(by_ats, ats, cost=cost, is_applied=is_applied, is_terminal=is_terminal)
        if is_terminal and not is_applied:
            bucket = classify_failure_bucket(status, apply_error)
            _add_failure_bucket(by_failure_bucket, bucket, cost=cost)

    return LocalJobsSummary(
        touched=touched,
        applied=applied,
        by_ats=dict(sorted(by_ats.items())),
        by_failure_bucket=dict(sorted(by_failure_bucket.items())),
    )


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _status(value: Any) -> str:
    return str(value or "").strip().lower()


def _get(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if hasattr(row, "get"):
            value = row.get(name)  # type: ignore[attr-defined]
            if value is not None:
                return value
            continue
        try:
            value = row[name]
        except (KeyError, TypeError):
            continue
        if value is not None:
            return value
    return None


def _add_count_cost(
    summary: dict[str, CountCost],
    key: str,
    *,
    cost: float,
    is_applied: bool,
    is_terminal: bool,
) -> None:
    item = summary.setdefault(key, CountCost())
    item.count += 1
    item.applied += int(is_applied)
    item.terminal += int(is_terminal)
    item.cost += cost


def _add_failure_bucket(summary: dict[str, FailureBucket], key: str, *, cost: float) -> None:
    item = summary.setdefault(key, FailureBucket())
    item.count += 1
    item.cost += cost


def _divide(numerator: float, denominator: int) -> float:
    if not denominator:
        return 0.0
    return numerator / denominator
