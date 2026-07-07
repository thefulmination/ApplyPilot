from typer.testing import CliRunner

import applypilot.cli as cli
from applypilot.fleet.cost_quality_report import CostQualityReport, FleetQueueSummary, LocalJobsSummary


def test_apply_cost_report_command_prints_summary(monkeypatch):
    runner = CliRunner()
    report = CostQualityReport(
        fleet=FleetQueueSummary(
            applied=2,
            terminal_attempts=4,
            total_cost_usd=2.5,
            cost_per_applied_all_in=1.25,
            cost_per_terminal_attempt=0.625,
        ),
        local=LocalJobsSummary(touched=5, applied=3),
    )

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(
        "applypilot.fleet.cost_quality_report.build_report",
        lambda pg_dsn=None, sqlite_path=None: report,
    )
    monkeypatch.setattr(
        "applypilot.fleet.cost_quality_report.render_report_markdown",
        lambda r: "Cost per applied: $1.25",
    )

    result = runner.invoke(cli.app, ["apply-cost-report"])

    assert result.exit_code == 0
    assert "Cost per applied: $1.25" in result.output
