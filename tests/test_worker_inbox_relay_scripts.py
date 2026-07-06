from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_mac_worker_forces_inbox_relay_not_direct_gmail():
    script = (REPO / "run-worker-mac.sh").read_text(encoding="utf-8")
    setup = (REPO / "setup-mac-worker.sh").read_text(encoding="utf-8")

    assert "export APPLYPILOT_INBOX_AUTH=1" in script
    assert "export APPLYPILOT_INBOX_AUTH_MODE=relay" in script
    assert "export APPLYPILOT_ENABLE_GMAIL_MCP=0" in script
    assert "hydrate-gmail.py" not in script
    assert "APPLYPILOT_INBOX_AUTH='1'" in setup
    assert "APPLYPILOT_INBOX_AUTH_MODE='relay'" in setup
    assert "APPLYPILOT_ENABLE_GMAIL_MCP='0'" in setup
