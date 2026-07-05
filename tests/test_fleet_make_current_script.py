from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_make_current_script_pins_reconciles_and_checks_health() -> None:
    script = (REPO / "Invoke-FleetMakeCurrent.ps1").read_text(encoding="utf-8")

    for text in (
        "codex/fleet-applier-hardening",
        "applypilot-hardening-and-brainstorm-integration",
        "current_sw_version",
        "set_pinned_version",
        "Invoke-FleetReconcile.ps1",
        "-Only Tarpon,GGGTower",
        "Test-NetConnection",
        "palomas-macbook-air",
        "Paloma is not reachable",
        "-Only Paloma",
        "fleet-health.ps1",
        "0.3.0+git.tree",
    ):
        assert text in script


def test_readme_points_to_make_current_as_primary_operator_command() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")

    assert "Invoke-FleetMakeCurrent.ps1" in readme
    assert "pins the current tree version" in readme
