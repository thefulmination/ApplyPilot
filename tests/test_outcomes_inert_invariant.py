# tests/test_outcomes_inert_invariant.py
import inspect

import applypilot.outcome_implied as implied
import applypilot.outcome_lane_signal as lane
import applypilot.cli as cli


def test_pure_modules_have_no_write_paths():
    """outcome_implied / outcome_lane_signal must never write the tracker, jobs, or scores."""
    for mod in (implied, lane):
        src = inspect.getsource(mod)
        assert "record_application" not in src
        assert "INSERT" not in src.upper()
        assert "UPDATE " not in src.upper()
        assert "conn.commit" not in src


def test_outcomes_promote_is_preview_only_in_source():
    """The promote command must not expose --apply or write to applications."""
    src = inspect.getsource(cli.outcomes_promote_command)
    assert "--apply" not in src
    assert "record_application" not in src
    assert "INSERT" not in src.upper()
    assert "UPDATE " not in src.upper()
