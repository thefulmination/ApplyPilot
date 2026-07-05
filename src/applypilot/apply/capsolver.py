"""Small CapSolver health helpers.

The apply agent still owns CAPTCHA solving inside the browser session. This
module is for deterministic configuration checks, so fleet boxes can prove the
CapSolver account is reachable before a worker burns an apply attempt.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import logging
import os
from typing import Any, Callable

import httpx

from applypilot import config


API_BASE = "https://api.capsolver.com"
GET_BALANCE_URL = f"{API_BASE}/getBalance"

logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass(frozen=True)
class CapSolverStatus:
    configured: bool
    ok: bool
    balance: float | None = None
    error_code: str | None = None
    error_description: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CapSolverFleetReadiness:
    configured: bool
    account_ok: bool
    prompt_fast_fail: bool
    ready: bool
    balance: float | None = None
    error_code: str | None = None
    error_description: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


PostFn = Callable[..., Any]
BalanceCheckFn = Callable[[], CapSolverStatus]


def _api_key() -> str:
    config.load_env()
    return (os.environ.get("CAPSOLVER_API_KEY") or "").strip()


def check_balance(*, timeout: float = 8.0, post: PostFn | None = None) -> CapSolverStatus:
    """Call CapSolver getBalance without exposing the API key in the result."""
    key = _api_key()
    if not key:
        return CapSolverStatus(
            configured=False,
            ok=False,
            error_code="missing_key",
            note="CAPSOLVER_API_KEY is not set.",
        )

    post_fn = post or httpx.post
    try:
        resp = post_fn(GET_BALANCE_URL, json={"clientKey": key}, timeout=timeout)
    except Exception as exc:
        return CapSolverStatus(
            configured=True,
            ok=False,
            error_code="network_error",
            error_description=str(exc),
            note="Could not reach CapSolver getBalance.",
        )

    try:
        payload = resp.json()
    except Exception:
        return CapSolverStatus(
            configured=True,
            ok=False,
            error_code=f"http_{getattr(resp, 'status_code', 'unknown')}",
            error_description=(getattr(resp, "text", "") or "")[:200],
            note="CapSolver returned a non-JSON response.",
        )

    if payload.get("errorId"):
        return CapSolverStatus(
            configured=True,
            ok=False,
            error_code=str(payload.get("errorCode") or "capsolver_error"),
            error_description=str(payload.get("errorDescription") or ""),
            note="CapSolver account check failed.",
        )

    raw_balance = payload.get("balance")
    try:
        balance = float(raw_balance)
    except (TypeError, ValueError):
        balance = None

    return CapSolverStatus(
        configured=True,
        ok=True,
        balance=balance,
        note="CapSolver account reachable.",
    )


def _captcha_section() -> str:
    from applypilot.apply import prompt

    return prompt._build_captcha_section()


def _has_prompt_fast_fail(captcha_section: str) -> bool:
    required = (
        "ERROR_INVALID_TASK_DATA",
        "ERROR_TASK_NOT_SUPPORTED",
        "RESULT:CAPTCHA",
    )
    return all(token in captcha_section for token in required)


def check_fleet_readiness(
    *,
    timeout: float = 8.0,
    balance_check: BalanceCheckFn | None = None,
    captcha_section: str | None = None,
) -> CapSolverFleetReadiness:
    """Return the full apply-worker CapSolver readiness gate.

    A fleet apply worker is CAPTCHA-capable only when the CapSolver account is
    reachable and the agent prompt has the fast-fail guard for unsupported
    CapSolver task responses. This keeps workers from burning attempts on
    challenge types the service already rejected.
    """
    account = balance_check() if balance_check else check_balance(timeout=timeout)
    try:
        section = captcha_section if captcha_section is not None else _captcha_section()
        prompt_fast_fail = _has_prompt_fast_fail(section)
    except Exception as exc:
        prompt_fast_fail = False
        if account.ok:
            return CapSolverFleetReadiness(
                configured=account.configured,
                account_ok=True,
                prompt_fast_fail=False,
                ready=False,
                balance=account.balance,
                error_code="prompt_check_error",
                error_description=str(exc),
                note="Could not verify the CapSolver prompt fast-fail guard.",
            )

    if not account.ok:
        return CapSolverFleetReadiness(
            configured=account.configured,
            account_ok=False,
            prompt_fast_fail=prompt_fast_fail,
            ready=False,
            balance=account.balance,
            error_code=account.error_code,
            error_description=account.error_description,
            note=account.note,
        )

    if not prompt_fast_fail:
        return CapSolverFleetReadiness(
            configured=account.configured,
            account_ok=True,
            prompt_fast_fail=False,
            ready=False,
            balance=account.balance,
            error_code="prompt_missing_fast_fail",
            note="Apply prompt is missing the unsupported-CapSolver fast-fail guard.",
        )

    return CapSolverFleetReadiness(
        configured=account.configured,
        account_ok=True,
        prompt_fast_fail=True,
        ready=True,
        balance=account.balance,
        note="CapSolver fleet readiness passed.",
    )
