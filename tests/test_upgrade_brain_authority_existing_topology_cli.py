from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "upgrade-brain-authority-existing-topology.py"


def test_cli_pins_candidate_source_before_stale_editable(tmp_path: Path) -> None:
    stale = tmp_path / "stale"
    package = stale / "applypilot" / "fleet"
    package.mkdir(parents=True)
    (stale / "applypilot" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "pg_roles.py").write_text(
        "raise RuntimeError('stale editable import won')\n", encoding="utf-8"
    )
    environment = {**os.environ, "PYTHONPATH": str(stale)}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "stale editable import won" not in result.stderr
    assert "--expected-database-name" in result.stdout
    assert "--expected-system-identifier" in result.stdout
