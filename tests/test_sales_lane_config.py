from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_config_accepts_sales_lane_path_overrides(tmp_path: Path) -> None:
    app_dir = tmp_path / "applypilot"
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(Path.cwd() / "src"),
        "APPLYPILOT_DIR": str(app_dir),
        "APPLYPILOT_DB_PATH": str(app_dir / "applypilot_sales.db"),
        "APPLYPILOT_RESUME_PATH": str(app_dir / "resume_sales.txt"),
        "APPLYPILOT_RESUME_STRATEGY_PATH": str(app_dir / "resume_strategy_sales.yaml"),
        "APPLYPILOT_SEARCH_CONFIG_PATH": str(app_dir / "searches_sales.yaml"),
    })

    code = """
from applypilot import config
print(config.DB_PATH)
print(config.RESUME_PATH)
print(config.RESUME_STRATEGY_PATH)
print(config.SEARCH_CONFIG_PATH)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    lines = [line.strip() for line in result.stdout.splitlines()]
    assert lines == [
        str(app_dir / "applypilot_sales.db"),
        str(app_dir / "resume_sales.txt"),
        str(app_dir / "resume_strategy_sales.yaml"),
        str(app_dir / "searches_sales.yaml"),
    ]
