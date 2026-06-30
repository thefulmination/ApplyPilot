"""Guard for the wall-clock bound used by timed continuous runs ("run for 5 hours").

The watchdog must (a) stop the run after the duration, (b) be a no-op when disabled,
and (c) return immediately if the run already stopped for another reason -- so a
cost-cap / breaker / Ctrl+C stop is never delayed by a pending long duration.
"""
from __future__ import annotations

import time

from applypilot.apply import launcher as L


def test_watchdog_sets_stop_event_after_timeout():
    L._stop_event.clear()
    try:
        L._duration_watchdog(0.3)
        assert L._stop_event.is_set()
    finally:
        L._stop_event.clear()


def test_watchdog_is_noop_when_disabled():
    L._stop_event.clear()
    L._duration_watchdog(0)
    assert not L._stop_event.is_set()


def test_watchdog_returns_immediately_if_already_stopped():
    # A long duration must NOT block when the run already stopped (cost cap / breaker /
    # Ctrl+C). It should early-return via the wait() returning True.
    L._stop_event.set()
    try:
        t0 = time.monotonic()
        L._duration_watchdog(100)  # would block ~100s if it didn't early-return
        assert time.monotonic() - t0 < 1.0
        assert L._stop_event.is_set()
    finally:
        L._stop_event.clear()
