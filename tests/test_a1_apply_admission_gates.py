from __future__ import annotations

import pytest
from typer.testing import CliRunner


def test_run_job_denies_before_job_execution(monkeypatch):
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

    with pytest.raises(SystemExit, match="run_job admission denied"):
        launcher.run_job({"url": "https://example.invalid/job"}, port=9222)

    assert admission_calls == [True]


@pytest.mark.parametrize("command", [["apply"], ["supervise-apply", "--max-cost-usd", "10"]])
def test_local_apply_commands_deny_before_bootstrap(monkeypatch, command):
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
    assert target_urls == [None]
