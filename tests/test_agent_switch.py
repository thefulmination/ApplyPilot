"""Tests for the dynamic Claude<->Codex apply-agent switcher.

Pure logic (clock injected), so no Postgres / no agent spend. The switcher decides
which apply agent a worker should use RIGHT NOW given each agent's independent
usage-limit window, and when a walled agent's window has reset.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from applypilot.fleet.agent_switch import AgentSwitcher, parse_reset_at


# --- AgentSwitcher: selection ------------------------------------------------

def test_fresh_switcher_uses_preferred():
    sw = AgentSwitcher("claude", "codex")
    assert sw.effective_agent(now=1000.0) == "claude"


def test_preferred_wall_falls_back_to_codex():
    sw = AgentSwitcher("claude", "codex", cooldown_seconds=3600)
    sw.note_wall("claude", now=1000.0)
    assert sw.effective_agent(now=1000.0) == "codex"


def test_no_fallback_wall_yields_none_so_driver_pauses():
    sw = AgentSwitcher("claude", fallback=None, cooldown_seconds=3600)
    sw.note_wall("claude", now=1000.0)
    assert sw.effective_agent(now=1000.0) is None


def test_both_walled_yields_none_and_resume_at_earlier_reset():
    sw = AgentSwitcher("claude", "codex", cooldown_seconds=3600)
    sw.note_wall("claude", now=1000.0, reset_at=5000.0)
    sw.note_wall("codex", now=1000.0, reset_at=4000.0)
    assert sw.effective_agent(now=1000.0) is None
    assert sw.resume_at(now=1000.0) == 4000.0


def test_wall_expires_and_preferred_comes_back():
    sw = AgentSwitcher("claude", "codex", cooldown_seconds=3600)
    sw.note_wall("claude", now=1000.0)  # blocked until 1000+3600
    assert sw.effective_agent(now=1000.0) == "codex"
    assert sw.effective_agent(now=4601.0) == "claude"


def test_explicit_reset_time_overrides_cooldown():
    sw = AgentSwitcher("claude", "codex", cooldown_seconds=3600)
    # reset is far past the default cooldown -> the parsed reset wins
    sw.note_wall("claude", now=1000.0, reset_at=20000.0)
    assert sw.blocked_until("claude") == 20000.0
    assert sw.effective_agent(now=4601.0) == "codex"   # cooldown would have freed it; reset has not


def test_sync_blocks_clears_stale_in_memory_wall():
    sw = AgentSwitcher("claude", "codex", cooldown_seconds=3600)
    sw.note_wall("claude", now=1000.0)
    sw.note_wall("codex", now=1000.0)
    sw.sync_blocks(10_000.0, {})  # no active blocks in DB anymore

    assert sw.effective_agent(now=10_000.0) == "claude"  # both cleared by sync
    assert sw.blocked_until("claude") == 0.0
    assert sw.blocked_until("codex") == 0.0


def test_past_reset_time_falls_back_to_cooldown():
    sw = AgentSwitcher("claude", "codex", cooldown_seconds=3600)
    sw.note_wall("claude", now=1000.0, reset_at=500.0)  # reset already in the past -> ignore
    assert sw.blocked_until("claude") == 1000.0 + 3600


def test_resume_at_none_when_an_agent_is_available():
    sw = AgentSwitcher("claude", "codex", cooldown_seconds=3600)
    sw.note_wall("claude", now=1000.0)
    assert sw.resume_at(now=1000.0) is None   # codex still serves


# --- AgentSwitcher: ordered N-agent chain (claude -> codex -> deepseek) ------

def test_ordered_chain_uses_first_agent():
    sw = AgentSwitcher(agents=["claude", "codex", "deepseek"])
    assert sw.effective_agent(now=1000.0) == "claude"


def test_ordered_chain_falls_through_to_third():
    sw = AgentSwitcher(agents=["claude", "codex", "deepseek"], cooldown_seconds=3600)
    sw.note_wall("claude", now=1000.0)
    assert sw.effective_agent(now=1000.0) == "codex"
    sw.note_wall("codex", now=1000.0)
    assert sw.effective_agent(now=1000.0) == "deepseek"


def test_ordered_chain_all_walled_pauses_until_earliest_reset():
    sw = AgentSwitcher(agents=["claude", "codex", "deepseek"], cooldown_seconds=3600)
    sw.note_wall("claude", now=1000.0, reset_at=9000.0)
    sw.note_wall("codex", now=1000.0, reset_at=6000.0)
    sw.note_wall("deepseek", now=1000.0, reset_at=7000.0)
    assert sw.effective_agent(now=1000.0) is None
    assert sw.resume_at(now=1000.0) == 6000.0


def test_ordered_chain_restores_preferred_after_reset():
    sw = AgentSwitcher(agents=["claude", "codex", "deepseek"], cooldown_seconds=3600)
    sw.note_wall("claude", now=1000.0)      # blocked until 4600
    assert sw.effective_agent(now=1000.0) == "codex"
    assert sw.effective_agent(now=4601.0) == "claude"


def test_preferred_fallback_form_still_works():
    # Back-compat: the (preferred, fallback) constructor is unchanged.
    sw = AgentSwitcher("claude", "codex", cooldown_seconds=3600)
    sw.note_wall("claude", now=1000.0)
    assert sw.effective_agent(now=1000.0) == "codex"


# --- parse_reset_at: "resets 12:40pm" / "try again at 3:15 PM" ---------------

def test_parse_reset_at_returns_same_day_when_still_ahead():
    now = datetime(2026, 7, 3, 11, 23, tzinfo=timezone.utc)
    got = parse_reset_at("You've hit your session limit · resets 12:40pm", now_local=now)
    assert got == datetime(2026, 7, 3, 12, 40, tzinfo=timezone.utc)


def test_parse_reset_at_shortens_just_passed_time():
    now = datetime(2026, 7, 3, 17, 10, 5, tzinfo=timezone.utc)
    got = parse_reset_at("try again at 5:10 PM", now_local=now)
    assert got == datetime(2026, 7, 3, 17, 12, 5, tzinfo=timezone.utc)


def test_parse_reset_at_does_not_roll_stale_try_again_time_to_tomorrow():
    now = datetime(2026, 7, 3, 18, 0, tzinfo=timezone.utc)
    got = parse_reset_at("try again at 5:10 PM", now_local=now)
    assert got is None


def test_parse_reset_at_handles_try_again_at_wording():
    now = datetime(2026, 7, 3, 11, 0, tzinfo=timezone.utc)
    got = parse_reset_at("Try again at 3:15 PM", now_local=now)
    assert got == datetime(2026, 7, 3, 15, 15, tzinfo=timezone.utc)


def test_parse_reset_at_handles_hour_only_reset():
    now = datetime(2026, 7, 5, 23, 30, tzinfo=timezone.utc)
    got = parse_reset_at("You've hit your weekly limit · resets 3am (America/New_York)", now_local=now)
    assert got == datetime(2026, 7, 6, 3, 0, tzinfo=timezone.utc)


def test_parse_reset_at_uses_latest_reset_mention():
    now = datetime(2026, 7, 5, 23, 30, tzinfo=timezone.utc)
    text = (
        "try again at 5:10 PM\n"
        "You've hit your weekly limit · resets 3am (America/New_York)"
    )
    got = parse_reset_at(text, now_local=now)
    assert got == datetime(2026, 7, 6, 3, 0, tzinfo=timezone.utc)


def test_parse_reset_at_none_when_absent():
    now = datetime(2026, 7, 3, 11, 0, tzinfo=timezone.utc)
    assert parse_reset_at("some unrelated crash text", now_local=now) is None
