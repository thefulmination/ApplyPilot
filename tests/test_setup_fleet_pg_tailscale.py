from pathlib import Path


def test_setup_fleet_pg_tailscale_uses_dict_row_access_for_pg_settings():
    script = Path("setup-fleet-pg-tailscale.ps1").read_text(encoding="utf-8")

    assert "fetchone()[0]" not in script
    assert "row['hba_file']" in script
    assert "row['listen_addresses']" in script
