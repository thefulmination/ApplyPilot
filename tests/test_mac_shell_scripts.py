"""bash -n parse gate for the macOS worker scripts (they can't run on Windows CI, but a
syntax error must not reach the Mac's self-update path — a broken wrapper bricks the
auto-update loop)."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = ["run-worker-mac.sh"]  # Task 6 appends "setup-mac-worker.sh"


def _find_bash():
    """A bash that can read Windows paths. On win32 the System32 WSL stub found by
    shutil.which cannot see C:\\ files (exit 127), so prefer Git Bash explicitly and
    otherwise skip; on POSIX plain `bash` is fine."""
    if sys.platform == "win32":
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env_name)
            if base:
                cand = Path(base) / "Git" / "bin" / "bash.exe"
                if cand.exists():
                    return str(cand)
        return None
    return shutil.which("bash")


@pytest.mark.parametrize("name", SCRIPTS)
def test_mac_shell_script_parses(name):
    bash = _find_bash()
    if not bash:
        pytest.skip("no usable bash (Git Bash on Windows / bash on POSIX)")
    script = str(REPO / name).replace("\\", "/")  # Git Bash prefers forward slashes
    r = subprocess.run([bash, "-n", script], capture_output=True, text=True)
    assert r.returncode == 0, f"{name} failed bash -n:\n{r.stderr}"
