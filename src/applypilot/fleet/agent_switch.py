"""Dynamic Claude<->Codex apply-agent switching for the residential fleet.

Claude and Codex draw on INDEPENDENT usage-limit pools (separate subscriptions), so
when Claude walls ("You've hit your session limit · resets 12:40pm") a worker can keep
applying on Codex until Claude's 5-hour rolling window resets, then switch back. This
module is the pure decision core (no I/O, clock injected); the driver in
apply_worker_main wires it to the worker loop.

Two pieces:
  - AgentSwitcher: given each agent's block-until time, pick the agent to use now (or
    None -> the driver pauses until the nearer reset).
  - parse_reset_at: pull the reset wall-clock time out of a usage-limit transcript so a
    walled agent is un-blocked at the ACTUAL reset, not a guessed cooldown.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

# "resets 12:40pm" (Claude session-limit wording, 2026-07-03+) or "try again at 3:15 PM"
# (older usage-limit wording). Case-insensitive; the two capture groups are alternatives.
_RESET_RE = re.compile(
    r"(?P<kind>try again at|resets)\s+(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m)",
    re.IGNORECASE,
)

_RESET_PAST_GRACE = timedelta(minutes=15)
_RESET_JUST_PASSED_DELAY = timedelta(minutes=2)


class AgentSwitcher:
    """Decide which apply agent a worker should use right now, given an ORDERED preference
    chain (e.g. claude -> codex -> deepseek). The first agent whose usage-limit window is
    open is used; when every agent is walled, effective_agent returns None and the driver
    pauses until resume_at(). A single-agent chain thus pauses until its own reset -- the
    "wary of the 5h limit" behavior for free.

    Construct with an ordered list -- AgentSwitcher(agents=["claude", "codex", "deepseek"]) --
    or the two-agent shorthand AgentSwitcher("claude", "codex").
    """

    def __init__(self, preferred: Optional[str] = None, fallback: Optional[str] = None,
                 *, agents: Optional[list[str]] = None, cooldown_seconds: float = 3600.0) -> None:
        if agents is not None:
            chain = [a for a in agents if a]
        else:
            chain = [a for a in (preferred, fallback) if a]
        if not chain:
            raise ValueError("AgentSwitcher needs at least one agent")
        self.agents = chain
        self.preferred = chain[0]
        self.fallback = chain[1] if len(chain) > 1 else None
        self.cooldown_seconds = float(cooldown_seconds)
        self._blocked_until: dict[str, float] = {}

    def blocked_until(self, agent: str) -> float:
        """Epoch until which `agent` is walled (0.0 if never walled)."""
        return self._blocked_until.get(agent, 0.0)

    def sync_blocks(self, now: float, blocks: dict[str, float]) -> None:
        """Reconcile switcher state with active fleet blocks from Postgres.

        `blocks` should include only currently active blocks (`agent -> blocked_until`
        where `blocked_until > now`). Any configured agent absent from `blocks` is treated
        as unblocked. This lets a long-running worker recover from stale in-memory walls
        when the controlling agent table has been reset upstream.
        """
        active = {str(agent): float(ts) for agent, ts in blocks.items()}
        for agent in self.agents:
            if active.get(agent, 0.0) > now:
                self._blocked_until[agent] = active[agent]
            else:
                self._blocked_until[agent] = 0.0

    def _available(self, agent: Optional[str], now: float) -> bool:
        return agent is not None and now >= self._blocked_until.get(agent, 0.0)

    def effective_agent(self, now: float) -> Optional[str]:
        for agent in self.agents:
            if self._available(agent, now):
                return agent
        return None

    def note_wall(self, agent: str, now: float, *, reset_at: Optional[float] = None) -> None:
        """Mark `agent` walled. Block until the parsed reset when it's in the future,
        else until now + cooldown (a re-wall just re-arms this)."""
        if reset_at is not None and reset_at > now:
            self._blocked_until[agent] = float(reset_at)
        else:
            self._blocked_until[agent] = now + self.cooldown_seconds

    def resume_at(self, now: float) -> Optional[float]:
        """When EVERY agent is walled, the earliest reset in the chain; else None (an agent
        is available now, so there is nothing to wait for)."""
        if self.effective_agent(now) is not None:
            return None
        times = [self._blocked_until.get(a, 0.0) for a in self.agents]
        return min(times) if times else None


def parse_reset_at(text: Optional[str], *, now_local: datetime) -> Optional[datetime]:
    """Parse the reset wall-clock time from a usage/session-limit transcript into the next
    datetime at or after now_local matching it (rolling to tomorrow if already passed).
    now_local carries the tz; the result inherits it. None if no reset time is present."""
    if not text:
        return None
    matches = list(_RESET_RE.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    kind = match.group("kind").lower()
    raw = match.group("time").strip()
    normalized = raw.upper().replace(" ", "")
    parsed = datetime.strptime(
        normalized,
        "%I:%M%p" if ":" in normalized else "%I%p",
    ).time()
    candidate = now_local.replace(hour=parsed.hour, minute=parsed.minute,
                                  second=0, microsecond=0)
    if candidate < now_local:
        if now_local - candidate <= _RESET_PAST_GRACE:
            return now_local + _RESET_JUST_PASSED_DELAY
        if kind == "try again at":
            return None
        candidate = candidate + timedelta(days=1)
    return candidate
