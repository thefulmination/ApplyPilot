from __future__ import annotations

from typer.testing import CliRunner

from applypilot import cli
from applypilot import linkedin_resolver


def test_linkedin_resolve_apply_urls_dry_run_wires_options(monkeypatch):
    captured = {}

    def fake_run_resolver(options):
        captured["options"] = options
        return linkedin_resolver.ResolverSummary(
            considered=3,
            dry_run=True,
            counts={},
            sample_urls=["https://www.linkedin.com/jobs/view/1"],
        )

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(linkedin_resolver, "run_resolver", fake_run_resolver)

    result = CliRunner().invoke(
        cli.app,
        [
            "linkedin-resolve-apply-urls",
            "--limit",
            "3",
            "--delay-min",
            "12",
            "--delay-max",
            "30",
            "--tiers",
            "priority,recommended",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert captured["options"].limit == 3
    assert captured["options"].delay_min == 12
    assert captured["options"].delay_max == 30
    assert captured["options"].tiers == ("priority", "recommended")
    assert captured["options"].dry_run is True
    assert "LinkedIn external apply URL resolver" in result.output
    assert "dry run" in result.output.lower()


def test_linkedin_resolve_apply_urls_rejects_empty_tiers(monkeypatch):
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)

    result = CliRunner().invoke(
        cli.app,
        ["linkedin-resolve-apply-urls", "--tiers", " , "],
    )

    assert result.exit_code == 1
    assert "--tiers must include at least one audit label" in result.output


def test_linkedin_resolve_apply_urls_rejects_bad_delay(monkeypatch):
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)

    result = CliRunner().invoke(
        cli.app,
        ["linkedin-resolve-apply-urls", "--delay-min", "30", "--delay-max", "12"],
    )

    assert result.exit_code == 1
    assert "--delay-max must be greater than or equal to --delay-min" in result.output
