from typer.testing import CliRunner

import applypilot.cli as cli

runner = CliRunner()


def test_outcomes_scan_renders_counts(monkeypatch):
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    import applypilot.outcome_scan as S
    monkeypatch.setattr(S, "scan_outcomes",
                        lambda **kw: {"inserted": 2, "skipped": 1, "updated": 0, "errors": 0})
    result = runner.invoke(cli.app, ["outcomes-scan", "--days", "10"])
    assert result.exit_code == 0
    assert "2" in result.stdout


def test_outcomes_dashboard_invokes_serve(monkeypatch):
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    called = {}
    import applypilot.outcome_dashboard as D
    monkeypatch.setattr(D, "serve", lambda **kw: called.update(kw))
    result = runner.invoke(cli.app, ["outcomes-dashboard", "--port", "9999", "--no-open"])
    assert result.exit_code == 0
    assert called.get("port") == 9999
