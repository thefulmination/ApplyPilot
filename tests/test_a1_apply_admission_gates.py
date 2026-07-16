from __future__ import annotations

import pytest
from typer.testing import CliRunner


def test_run_job_denies_before_job_execution(monkeypatch, capsys):
    from applypilot.apply import launcher
    from applypilot.fleet import emergency_admission

    admission_calls = []

    def deny():
        admission_calls.append(True)
        return emergency_admission.deny("run_job admission denied")

    monkeypatch.setattr(emergency_admission, "launcher_admission", deny)
    monkeypatch.setattr(
        launcher,
        "_run_job_impl",
        lambda *_args, **_kwargs: pytest.fail("job execution reached before admission"),
    )

    with pytest.raises(SystemExit) as exc_info:
        launcher.run_job({"url": "https://example.invalid/job"}, port=9222)

    assert exc_info.value.code == emergency_admission.DENIAL_EXIT_CODE
    assert emergency_admission.DENIAL_MARKER in capsys.readouterr().err
    assert admission_calls == [True]


def test_worker_loop_denies_before_state_queue_or_browser_side_effects(monkeypatch, capsys):
    from applypilot.apply import launcher
    from applypilot.fleet import emergency_admission

    monkeypatch.setattr(
        emergency_admission,
        "launcher_admission",
        lambda: emergency_admission.deny("worker loop admission denied"),
    )
    monkeypatch.setattr(
        launcher,
        "update_state",
        lambda *_args, **_kwargs: pytest.fail("state mutation reached before admission"),
    )
    monkeypatch.setattr(
        launcher,
        "acquire_job",
        lambda *_args, **_kwargs: pytest.fail("queue lease reached before admission"),
    )
    monkeypatch.setattr(
        launcher,
        "launch_chrome",
        lambda *_args, **_kwargs: pytest.fail("Chrome startup reached before admission"),
    )

    with pytest.raises(SystemExit) as exc_info:
        launcher.worker_loop()

    assert exc_info.value.code == emergency_admission.DENIAL_EXIT_CODE
    stderr = capsys.readouterr().err
    assert emergency_admission.DENIAL_MARKER in stderr
    assert "worker loop admission denied" in stderr


@pytest.mark.parametrize(
    ("command", "target_url"),
    [
        (["apply"], None),
        (["apply", "--url", "https://example.invalid/job"], "https://example.invalid/job"),
        (["apply", "--dry-run"], None),
        (["apply", "--auth-gated"], None),
        (["supervise-apply", "--max-cost-usd", "10"], None),
    ],
)
def test_local_apply_commands_deny_before_bootstrap(monkeypatch, command, target_url):
    from applypilot import cli
    from applypilot.fleet import emergency_admission

    target_urls = []

    def deny(*, target_url=None):
        target_urls.append(target_url)
        return emergency_admission.deny("local apply admission denied")

    monkeypatch.setattr(emergency_admission, "local_apply_admission", deny)
    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: pytest.fail("bootstrap reached before admission"),
    )

    result = CliRunner().invoke(cli.app, command)

    assert result.exit_code == emergency_admission.DENIAL_EXIT_CODE
    assert emergency_admission.DENIAL_MARKER in result.output
    assert "local apply admission denied" in result.output
    assert target_urls == [target_url]


def test_mark_failed_and_reset_failed_bypass_acquisition_admission(monkeypatch):
    from applypilot import cli
    from applypilot.apply import launcher
    from applypilot.fleet import emergency_admission

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(
        emergency_admission,
        "local_apply_admission",
        lambda **_kwargs: pytest.fail("utility mode reached acquisition admission"),
    )
    marked = []
    monkeypatch.setattr(launcher, "mark_job", lambda *args, **kwargs: marked.append((args, kwargs)))
    monkeypatch.setattr(launcher, "reset_failed", lambda: 4)

    failed = CliRunner().invoke(
        cli.app,
        ["apply", "--mark-failed", "https://example.invalid/job", "--fail-reason", "manual"],
    )
    reset = CliRunner().invoke(cli.app, ["apply", "--reset-failed"])

    assert failed.exit_code == 0, failed.output
    assert reset.exit_code == 0, reset.output
    assert marked == [(("https://example.invalid/job", "failed"), {"reason": "manual"})]
    assert "Reset 4 failed job(s)" in reset.output


def test_gen_bypasses_acquisition_admission(monkeypatch, tmp_path):
    from applypilot import cli, config
    from applypilot.apply import launcher
    from applypilot.fleet import emergency_admission

    prompt_path = tmp_path / "prompt.txt"
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(
        emergency_admission,
        "local_apply_admission",
        lambda **_kwargs: pytest.fail("--gen reached acquisition admission"),
    )
    monkeypatch.setattr(config, "check_tier", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(type(config.PROFILE_PATH), "exists", lambda _self: True, raising=False)
    monkeypatch.setattr(launcher, "gen_prompt", lambda *_args, **_kwargs: prompt_path)
    monkeypatch.setattr(launcher, "build_apply_agent_command", lambda **_kwargs: ["agent"])

    result = CliRunner().invoke(
        cli.app,
        ["apply", "--gen", "--url", "https://example.invalid/job", "--agent", "codex"],
    )

    assert result.exit_code == 0, result.output
    assert "Wrote prompt to:" in result.output
    assert prompt_path.name in result.output
