from datetime import datetime, timezone

from applypilot.fleet.agent_readiness import blocked_desired_agent_chains


def test_claude_block_does_not_block_claude_worker_with_codex_fallback():
    desired = [
        {"machine_owner": "m4", "desired_workers": 4, "agent": "claude"},
    ]
    active_blocks = {
        "claude": {
            "blocked_until": datetime(2026, 7, 6, 3, 0, tzinfo=timezone.utc),
            "reason": "usage_limit_wall",
        }
    }

    assert blocked_desired_agent_chains(desired, active_blocks) == []


def test_desired_worker_blocks_when_every_agent_in_chain_is_blocked():
    desired = [
        {"machine_owner": "m4", "desired_workers": 4, "agent": "claude"},
    ]
    active_blocks = {
        "claude": {
            "blocked_until": datetime(2026, 7, 6, 3, 0, tzinfo=timezone.utc),
            "reason": "usage_limit_wall",
        },
        "codex": {
            "blocked_until": datetime(2026, 7, 6, 4, 46, tzinfo=timezone.utc),
            "reason": "usage_limit_wall",
        },
    }

    blockers = blocked_desired_agent_chains(desired, active_blocks)

    assert len(blockers) == 1
    assert "m4 desired agent chain claude,codex is fully blocked" in blockers[0]
