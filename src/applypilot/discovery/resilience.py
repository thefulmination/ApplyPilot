"""Scraper resilience: jittered exponential backoff + per-board circuit breakers.

Circuit breakers are in-memory and reset each process — they prevent runaway
requests WITHIN a single discovery run when a board is clearly down. The
corporate ATS negative-token cache already handles cross-run dead-token skipping;
these two mechanisms complement each other.
"""

from __future__ import annotations

import logging
import random
import threading
import time

log = logging.getLogger(__name__)


def jitter_backoff(attempt: int, base: float = 2.0, cap: float = 60.0) -> float:
    """Full-jitter exponential backoff.

    Returns a float in [0, min(cap, base * 2^(attempt-1))].

    Full-jitter (vs decorrelated) because the scrapers run single-threaded per
    board — the primary goal is to avoid deterministic retry timing that triggers
    rate-limit detection, not to minimise per-request latency variance.
    """
    ceiling = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(0.0, ceiling)


class CircuitBreaker:
    """Per-source circuit breaker: CLOSED → OPEN → HALF_OPEN → CLOSED.

    Trips after `failure_threshold` consecutive errors, then blocks all calls
    for `reset_timeout` seconds, then allows one probe request (HALF_OPEN).
    A probe success closes the circuit; a probe failure re-opens it.

    Thread-safe: shared across threads in the corporate ATS ThreadPoolExecutor.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout: float = 60.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at: float = 0.0
        self._half_open_probe_sent: bool = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._current_state()

    def _current_state(self) -> str:
        """Compute effective state (may auto-transition OPEN→HALF_OPEN). Lock held."""
        if self._state == self.OPEN:
            if time.monotonic() - self._opened_at >= self.reset_timeout:
                return self.HALF_OPEN
        return self._state

    def is_open(self) -> bool:
        """Return True if this request should be blocked."""
        with self._lock:
            state = self._current_state()
            if state == self.OPEN:
                return True
            if state == self.HALF_OPEN:
                # Allow exactly one probe through; subsequent calls block until result
                if self._half_open_probe_sent:
                    return True
                self._half_open_probe_sent = True
            return False

    def record_success(self) -> None:
        with self._lock:
            prev = self._current_state()
            if prev != self.CLOSED:
                log.info("[circuit-breaker] CLOSED (recovered after %d failures)", self._failures)
            self._state = self.CLOSED
            self._failures = 0
            self._half_open_probe_sent = False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            state = self._current_state()
            if state == self.HALF_OPEN or self._failures >= self.failure_threshold:
                if self._state != self.OPEN:
                    log.warning(
                        "[circuit-breaker] OPEN after %d failure(s) — blocking for %.0fs",
                        self._failures,
                        self.reset_timeout,
                    )
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                self._half_open_probe_sent = False


_REGISTRY: dict[str, CircuitBreaker] = {}
_REGISTRY_LOCK = threading.Lock()


def get_board_breaker(
    board: str,
    *,
    failure_threshold: int = 5,
    reset_timeout: float = 60.0,
) -> CircuitBreaker:
    """Return the shared CircuitBreaker for a named board, creating if absent."""
    with _REGISTRY_LOCK:
        if board not in _REGISTRY:
            _REGISTRY[board] = CircuitBreaker(failure_threshold, reset_timeout)
        return _REGISTRY[board]
