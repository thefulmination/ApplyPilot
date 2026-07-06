from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


NY = ZoneInfo("America/New_York")


def test_m4_blackout_active_during_weekday_work_hours() -> None:
    from applypilot.fleet.work_hours import apply_blackout_active

    assert apply_blackout_active("m4", now=datetime(2026, 7, 6, 8, 0, tzinfo=NY)) is True
    assert apply_blackout_active("m4", now=datetime(2026, 7, 6, 16, 59, tzinfo=NY)) is True


def test_m4_blackout_inactive_after_hours_and_weekends() -> None:
    from applypilot.fleet.work_hours import apply_blackout_active

    assert apply_blackout_active("m4", now=datetime(2026, 7, 6, 7, 59, tzinfo=NY)) is False
    assert apply_blackout_active("m4", now=datetime(2026, 7, 6, 17, 0, tzinfo=NY)) is False
    assert apply_blackout_active("m4", now=datetime(2026, 7, 11, 10, 0, tzinfo=NY)) is False


def test_blackout_only_targets_m4_and_honors_override() -> None:
    from applypilot.fleet.work_hours import apply_blackout_active

    now = datetime(2026, 7, 6, 10, 0, tzinfo=NY)

    assert apply_blackout_active("m2", now=now) is False
    assert apply_blackout_active("m4", now=now, allow_override=True) is False
