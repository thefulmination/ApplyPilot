"""Reactive subscription rate-governor (home-side). Spend is not a constraint; this
exists to (a) not hammer after a limit signal (the abuse pattern), (b) pace serial
calls, (c) optionally bound a window when sharing a dev account."""
from __future__ import annotations

import json
import time


class FrontierGovernor:
    def __init__(self, account, *, min_gap_seconds=0.0, window_seconds=None,
                 window_budget=None, state_path=None, _now=time.monotonic):
        self.account = account
        self.min_gap = float(min_gap_seconds)
        self.window_seconds = window_seconds
        self.window_budget = window_budget
        self.state_path = state_path
        self._now = _now
        self._last_call = None
        self._tripped_until = None
        self._window_start = self._now()
        self._window_count = 0
        self._load()

    def _roll(self):
        if self.window_seconds and (self._now() - self._window_start) >= self.window_seconds:
            self._window_start = self._now()
            self._window_count = 0

    def allow(self) -> bool:
        self._roll()
        now = self._now()
        if self._tripped_until is not None:
            if now < self._tripped_until:
                return False
            self._tripped_until = None
        if self._last_call is not None and (now - self._last_call) < self.min_gap:
            return False
        if self.window_budget is not None and self._window_count >= self.window_budget:
            return False
        return True

    def record(self, outcome, *, now=None) -> None:
        t = now if now is not None else self._now()
        self._last_call = t
        self._roll()
        self._window_count += 1
        if outcome == "limit":
            self._tripped_until = t + float(self.window_seconds or 300)
        self._save()

    def _load(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                s = json.load(fh)
            self._tripped_until = s.get("tripped_until")
        except (OSError, ValueError):
            pass

    def _save(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump({"tripped_until": self._tripped_until}, fh)
        except OSError:
            pass
