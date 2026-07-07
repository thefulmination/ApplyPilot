"""The default apply agent is Codex, not Claude.

Flipping the default moves fleet + CLI apply runs onto the ChatGPT (Codex)
quota pool by default, off the Claude Max subscription. Callers can still pass
--agent claude explicitly, and APPLYPILOT_FALLBACK_AGENT / --fallback-agent
control spillover.
"""

import inspect

from applypilot import cli
from applypilot.fleet import apply_worker_main as awm
from applypilot.fleet.apply_worker_main import build_parser


def test_fleet_apply_worker_defaults_to_codex():
    args = build_parser().parse_args(["--worker-id", "w0"])
    assert args.agent == "codex"


def test_cli_apply_defaults_to_codex():
    default = inspect.signature(cli.apply).parameters["agent"].default
    # typer wraps option defaults in an OptionInfo whose .default holds the value
    assert getattr(default, "default", default) == "codex"


def test_fleet_apply_worker_once_limits_loop_iterations(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_LABEL", "mint")
    monkeypatch.setattr(awm, "build_apply_loop", lambda **_kw: object())
    monkeypatch.setattr(awm, "install_stop_handler", lambda: None)
    monkeypatch.setattr(awm, "make_apply_fn", lambda *_a, **_kw: (lambda _job: {}))

    captured = {}

    def fake_run_apply(_conn_factory, _loop, **kwargs):
        captured["max_iterations"] = kwargs.get("max_iterations")
        return {"applied": 0, "halted": 0, "idle": 0, "error": 0}

    monkeypatch.setattr(awm, "run_apply", fake_run_apply)

    assert awm.main([
        "--dsn", "postgresql://example",
        "--worker-id", "mint-0",
        "--machine-owner", "mint",
        "--once",
    ]) == 0

    assert captured["max_iterations"] == 1


def test_fleet_apply_worker_max_iterations_limits_loop_iterations(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_LABEL", "mint")
    monkeypatch.setattr(awm, "build_apply_loop", lambda **_kw: object())
    monkeypatch.setattr(awm, "install_stop_handler", lambda: None)
    monkeypatch.setattr(awm, "make_apply_fn", lambda *_a, **_kw: (lambda _job: {}))

    captured = {}

    def fake_run_apply(_conn_factory, _loop, **kwargs):
        captured["max_iterations"] = kwargs.get("max_iterations")
        return {"applied": 0, "halted": 0, "idle": 0, "error": 0}

    monkeypatch.setattr(awm, "run_apply", fake_run_apply)

    assert awm.main([
        "--dsn", "postgresql://example",
        "--worker-id", "mint-0",
        "--machine-owner", "mint",
        "--max-iterations", "3",
    ]) == 0

    assert captured["max_iterations"] == 3
