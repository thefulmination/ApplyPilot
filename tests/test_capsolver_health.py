from __future__ import annotations

import json
import logging


def test_capsolver_health_quiets_httpx_info_logger() -> None:
    from applypilot.apply import capsolver

    assert logging.getLogger("httpx").level >= logging.WARNING


def test_capsolver_balance_check_reports_missing_key(monkeypatch) -> None:
    from applypilot.apply import capsolver

    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    monkeypatch.setattr(capsolver.config, "load_env", lambda: None)

    status = capsolver.check_balance()

    assert status.configured is False
    assert status.ok is False
    assert status.error_code == "missing_key"
    assert status.balance is None


def test_capsolver_balance_check_calls_get_balance_without_leaking_key(monkeypatch) -> None:
    from applypilot.apply import capsolver

    secret = "CAI-this-secret-must-not-be-printed"
    calls: list[tuple[str, dict, float]] = []

    class Response:
        status_code = 200
        text = '{"errorId":0,"balance":12.34}'

        def json(self) -> dict:
            return {"errorId": 0, "balance": 12.34}

    def fake_post(url: str, *, json: dict, timeout: float):
        calls.append((url, json, timeout))
        return Response()

    monkeypatch.setenv("CAPSOLVER_API_KEY", secret)
    monkeypatch.setattr(capsolver.config, "load_env", lambda: None)

    status = capsolver.check_balance(timeout=3.0, post=fake_post)

    assert status.configured is True
    assert status.ok is True
    assert status.balance == 12.34
    assert calls == [
        ("https://api.capsolver.com/getBalance", {"clientKey": secret}, 3.0)
    ]
    assert secret not in status.note


def test_capsolver_balance_check_surfaces_api_error(monkeypatch) -> None:
    from applypilot.apply import capsolver

    class Response:
        status_code = 200
        text = '{"errorId":1,"errorCode":"ERROR_ZERO_BALANCE","errorDescription":"Insufficient account balance"}'

        def json(self) -> dict:
            return {
                "errorId": 1,
                "errorCode": "ERROR_ZERO_BALANCE",
                "errorDescription": "Insufficient account balance",
            }

    monkeypatch.setenv("CAPSOLVER_API_KEY", "CAI-secret")
    monkeypatch.setattr(capsolver.config, "load_env", lambda: None)

    status = capsolver.check_balance(post=lambda *args, **kwargs: Response())

    assert status.configured is True
    assert status.ok is False
    assert status.error_code == "ERROR_ZERO_BALANCE"
    assert status.error_description == "Insufficient account balance"


def test_capsolver_check_json_is_single_line(monkeypatch) -> None:
    from typer.testing import CliRunner

    from applypilot import cli
    from applypilot.apply import capsolver

    monkeypatch.setattr(
        capsolver,
        "check_balance",
        lambda: capsolver.CapSolverStatus(
            configured=True,
            ok=True,
            balance=9.99,
            note="CapSolver account reachable.",
        ),
    )

    result = CliRunner().invoke(cli.app, ["capsolver-check", "--json"])

    assert result.exit_code == 0
    assert result.output.count("\n") == 1
    assert json.loads(result.output)["balance"] == 9.99
