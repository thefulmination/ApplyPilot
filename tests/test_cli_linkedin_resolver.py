from __future__ import annotations

from typer.testing import CliRunner

from applypilot import cli
from applypilot import company_resolver
from applypilot import indeed_resolver
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


def test_indeed_resolve_apply_urls_dry_run_wires_options(monkeypatch):
    captured = {}

    def fake_run_resolver(options):
        captured["options"] = options
        return indeed_resolver.IndeedResolverSummary(
            considered=2,
            dry_run=True,
            counts={"resolved_offsite": 2},
            sample_urls=["https://www.indeed.com/viewjob?jk=1"],
        )

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(indeed_resolver, "run_resolver", fake_run_resolver)

    result = CliRunner().invoke(
        cli.app,
        [
            "indeed-resolve-apply-urls",
            "--limit",
            "2",
            "--tiers",
            "priority,recommended",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert captured["options"].limit == 2
    assert captured["options"].tiers == ("priority", "recommended")
    assert captured["options"].dry_run is True
    assert "Indeed apply URL resolver" in result.output
    assert "dry run" in result.output.lower()
    assert "resolved_offsite: 2" in result.output


def test_indeed_resolve_apply_urls_rejects_empty_tiers(monkeypatch):
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)

    result = CliRunner().invoke(
        cli.app,
        ["indeed-resolve-apply-urls", "--tiers", " , "],
    )

    assert result.exit_code == 1
    assert "--tiers must include at least one audit label" in result.output


def test_indeed_resolve_apply_urls_rejects_negative_limit(monkeypatch):
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)

    result = CliRunner().invoke(
        cli.app,
        ["indeed-resolve-apply-urls", "--limit", "-1"],
    )

    assert result.exit_code == 1
    assert "--limit must be 0 or a positive number" in result.output


def test_boost_output_resolves_urls_and_generates_until_ready_target(monkeypatch):
    calls = {"pipeline": 0, "liveness": 0, "company": 0, "indeed": 0}
    ready_values = [10, 80, 120, 150]

    def fake_get_stats():
        if len(ready_values) > 1:
            return {"ready_to_apply": ready_values.pop(0)}
        return {"ready_to_apply": ready_values[0]}

    def fake_company(options):
        calls["company"] += 1
        assert isinstance(options, company_resolver.CompanyResolverOptions)
        assert options.limit == 2000
        return company_resolver.CompanyResolverSummary(
            considered=50,
            counts={"resolved_company_match": 2},
        )

    def fake_indeed(options):
        calls["indeed"] += 1
        assert isinstance(options, indeed_resolver.IndeedResolverOptions)
        assert options.limit == 2000
        return indeed_resolver.IndeedResolverSummary(
            considered=40,
            counts={"resolved_offsite": 5, "hosted_apply": 3, "unresolved": 2},
        )

    def fake_verify_jobs(*args, **kwargs):
        calls["liveness"] += 1
        assert kwargs["limit"] == 500
        assert kwargs["workers"] == 16
        return {"candidates": 500, "checked": 500, "skipped_fresh": 0, "by_status": {"live": 500}}

    def fake_run_pipeline(*, stages, min_score, batch_size, validation_mode, workers, generation_workers, **kwargs):
        calls["pipeline"] += 1
        assert stages == ["tailor", "cover", "pdf"]
        assert min_score == 7
        assert batch_size == 500
        assert validation_mode == "lenient"
        assert workers == 1
        assert generation_workers == 4
        return {"errors": {}, "stages": []}

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr("applypilot.company_resolver.run_resolver", fake_company)
    monkeypatch.setattr("applypilot.indeed_resolver.run_resolver", fake_indeed)
    monkeypatch.setattr("applypilot.database.get_connection", lambda: object())
    monkeypatch.setattr("applypilot.database.get_stats", fake_get_stats)
    monkeypatch.setattr("applypilot.apply.liveness.verify_jobs", fake_verify_jobs)
    monkeypatch.setattr("applypilot.pipeline.run_pipeline", fake_run_pipeline)

    result = CliRunner().invoke(
        cli.app,
        [
            "boost-output",
            "--target-ready",
            "150",
            "--batch-size",
            "500",
            "--company-limit",
            "2000",
            "--verify-limit",
            "500",
            "--generation-workers",
            "4",
        ],
    )

    assert result.exit_code == 0
    assert calls == {"pipeline": 2, "liveness": 1, "company": 1, "indeed": 1}
    assert "Indeed URL pass" in result.output
    assert "ApplyPilot output boost complete" in result.output
