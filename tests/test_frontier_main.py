# tests/test_frontier_main.py
"""Guard test: --use-subscription without --enable-subscription must raise SystemExit."""
import pytest
from applypilot.fleet import frontier_main as fm


def test_subscription_requires_explicit_enable():
    """Requesting the subscription backend without the explicit opt-in flag must
    raise SystemExit BEFORE touching the brain or any subprocess."""
    with pytest.raises(SystemExit, match="enable-subscription"):
        fm.main(["--use-subscription"])
