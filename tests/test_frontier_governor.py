# tests/test_frontier_governor.py
from applypilot.fleet.frontier_governor import FrontierGovernor


def test_allow_min_gap_and_limit_trip():
    t = {"v": 1000.0}
    g = FrontierGovernor("codex", min_gap_seconds=10, window_seconds=300, _now=lambda: t["v"])
    assert g.allow() is True
    g.record("ok")
    assert g.allow() is False              # within min-gap
    t["v"] += 11
    assert g.allow() is True               # past the gap
    g.record("limit")                       # tripped for the window
    assert g.allow() is False
    t["v"] += 301
    assert g.allow() is True                # window elapsed -> recovered


def test_window_budget_optional_bound():
    t = {"v": 0.0}
    g = FrontierGovernor("codex", min_gap_seconds=0, window_seconds=100, window_budget=2, _now=lambda: t["v"])
    g.record("ok"); g.record("ok")
    assert g.allow() is False               # budget spent this window
    t["v"] += 101
    assert g.allow() is True                # window rolled
