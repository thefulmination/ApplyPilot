"""Tests for applypilot.discovery.resilience (jitter backoff + circuit breaker)."""

from __future__ import annotations

import threading
import time

import pytest

from applypilot.discovery.resilience import (
    CircuitBreaker,
    get_board_breaker,
    jitter_backoff,
)


# ---------------------------------------------------------------------------
# jitter_backoff
# ---------------------------------------------------------------------------

class TestJitterBackoff:
    def test_returns_non_negative(self):
        for attempt in range(1, 6):
            assert jitter_backoff(attempt) >= 0.0

    def test_bounded_by_cap(self):
        for attempt in range(1, 10):
            assert jitter_backoff(attempt, cap=5.0) <= 5.0

    def test_first_attempt_bounded_by_base(self):
        # attempt=1: ceiling = min(cap, base * 2^0) = base
        for _ in range(20):
            assert jitter_backoff(1, base=3.0, cap=100.0) <= 3.0

    def test_second_attempt_bounded_by_2x_base(self):
        # attempt=2: ceiling = min(cap, base * 2^1) = 2*base (if cap allows)
        for _ in range(20):
            assert jitter_backoff(2, base=2.0, cap=100.0) <= 4.0

    def test_caps_at_cap(self):
        # Very high attempt: ceiling should be capped
        for _ in range(20):
            assert jitter_backoff(100, base=2.0, cap=10.0) <= 10.0

    def test_zero_attempt_does_not_error(self):
        val = jitter_backoff(0, base=2.0)
        assert val >= 0.0

    def test_randomness(self):
        # Should not always return the same value
        values = {jitter_backoff(3, base=5.0, cap=60.0) for _ in range(30)}
        assert len(values) > 1


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def test_initial_state_is_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.is_open() is False

    def test_does_not_open_before_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.is_open() is False

    def test_opens_at_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.is_open() is True

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        # Only 2 failures after the reset — should still be closed
        assert cb.state == CircuitBreaker.CLOSED

    def test_transitions_to_half_open_after_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.01)
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        time.sleep(0.05)
        # After timeout, state transitions to half-open
        assert cb.state == CircuitBreaker.HALF_OPEN

    def test_half_open_allows_one_probe(self):
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.01)
        cb.record_failure()
        time.sleep(0.05)
        # First call should get through (probe)
        assert cb.is_open() is False
        # Second call should be blocked while probe is pending
        assert cb.is_open() is True

    def test_probe_success_closes_circuit(self):
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.01)
        cb.record_failure()
        time.sleep(0.05)
        cb.is_open()  # consume the probe slot
        cb.record_success()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.is_open() is False

    def test_probe_failure_reopens_circuit(self):
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.01)
        cb.record_failure()
        time.sleep(0.05)
        cb.is_open()  # consume the probe slot
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.is_open() is True

    def test_multiple_successes_keep_closed(self):
        cb = CircuitBreaker(failure_threshold=5)
        for _ in range(10):
            cb.record_success()
        assert cb.state == CircuitBreaker.CLOSED

    def test_thread_safe_concurrent_failures(self):
        cb = CircuitBreaker(failure_threshold=10)
        errors: list[Exception] = []

        def fail_loop() -> None:
            try:
                for _ in range(5):
                    cb.record_failure()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=fail_loop) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert cb.state == CircuitBreaker.OPEN


# ---------------------------------------------------------------------------
# get_board_breaker registry
# ---------------------------------------------------------------------------

class TestGetBoardBreaker:
    def test_returns_circuit_breaker(self):
        cb = get_board_breaker("test_board_unique_xyz")
        assert isinstance(cb, CircuitBreaker)

    def test_same_board_same_instance(self):
        cb1 = get_board_breaker("board_registry_test_abc")
        cb2 = get_board_breaker("board_registry_test_abc")
        assert cb1 is cb2

    def test_different_boards_different_instances(self):
        cb1 = get_board_breaker("board_a_unique")
        cb2 = get_board_breaker("board_b_unique")
        assert cb1 is not cb2

    def test_custom_threshold_and_timeout(self):
        cb = get_board_breaker(
            "board_custom_params_unique",
            failure_threshold=2,
            reset_timeout=120.0,
        )
        assert cb.failure_threshold == 2
        assert cb.reset_timeout == 120.0
