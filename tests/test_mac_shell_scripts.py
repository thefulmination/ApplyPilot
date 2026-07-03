"""bash -n parse gate for the macOS worker scripts (they can't run on Windows CI, but a
syntax error must not reach the Mac's self-update path — a broken wrapper bricks the
auto-update loop)."""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = ["run-worker-mac.sh"]  # Task 6 appends "setup-mac-worker.sh"


@pytest.mark.parametrize("name", SCRIPTS)
def test_mac_shell_script_parses(name):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not on PATH")
    r = subprocess.run([bash, "-n", str(REPO / name)], capture_output=True, text=True)
    assert r.returncode == 0, f"{name} failed bash -n:\n{r.stderr}"
