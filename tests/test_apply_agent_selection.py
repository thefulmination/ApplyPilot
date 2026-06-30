from __future__ import annotations

from pathlib import Path

import pytest


def test_tier3_accepts_codex_when_claude_is_missing(monkeypatch) -> None:
    from applypilot import config

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "get_chrome_path", lambda: "chrome.exe")

    def missing_claude() -> str:
        raise FileNotFoundError("no claude")

    monkeypatch.setattr(config, "get_claude_path", missing_claude)
    monkeypatch.setattr(config, "get_codex_path", lambda: "codex.exe")

    assert config.get_tier() == 3


def test_get_codex_path_ignores_unrunnable_alias(monkeypatch, tmp_path: Path) -> None:
    from applypilot import config

    alias = tmp_path / "codex.exe"
    alias.write_text("not runnable", encoding="utf-8")
    monkeypatch.delenv("CODEX_PATH", raising=False)
    monkeypatch.setattr(config.shutil, "which", lambda name: str(alias) if name == "codex" else None)

    def cannot_execute(*args, **kwargs):
        raise PermissionError("Access is denied")

    monkeypatch.setattr(config.subprocess, "run", cannot_execute)

    with pytest.raises(FileNotFoundError):
        config.get_codex_path()


def test_build_apply_agent_command_defaults_to_claude(monkeypatch, tmp_path: Path) -> None:
    from applypilot.apply import launcher

    monkeypatch.setattr(launcher.config, "get_claude_path", lambda: "claude.exe")
    cmd = launcher.build_apply_agent_command(
        agent="claude",
        model="sonnet",
        mcp_config_path=tmp_path / ".mcp-apply-0.json",
        cdp_port=9222,
    )

    assert cmd[:3] == ["claude.exe", "--model", "sonnet"]
    assert "--mcp-config" in cmd
    assert "--output-format" in cmd and "stream-json" in cmd
    assert cmd[-1] == "-"


def test_build_apply_agent_command_supports_codex_exec(monkeypatch, tmp_path: Path) -> None:
    from applypilot.apply import launcher

    monkeypatch.setattr(launcher.config, "get_codex_path", lambda: "codex.exe")
    cmd = launcher.build_apply_agent_command(
        agent="codex",
        model="gpt-5.5",
        mcp_config_path=tmp_path / ".mcp-apply-0.json",
        cdp_port=9333,
    )

    assert cmd[:4] == ["codex.exe", "exec", "--model", "gpt-5.5"]
    assert "--json" in cmd
    assert "--ignore-user-config" in cmd
    assert "--ignore-rules" in cmd
    assert cmd.count("--disable") == 3
    assert {"plugins", "apps", "memories"}.issubset(set(cmd))
    assert "--sandbox" in cmd and "read-only" in cmd
    assert any("skills.config=" in item for item in cmd)
    assert "--mcp-config" not in cmd
    assert any("mcp_servers.playwright.command" in item for item in cmd)
    assert any("--cdp-endpoint=http://localhost:9333" in item for item in cmd)
    assert cmd[-1] == "-"


def test_build_apply_agent_command_allows_codex_default_model(monkeypatch, tmp_path: Path) -> None:
    from applypilot.apply import launcher

    monkeypatch.setattr(launcher.config, "get_codex_path", lambda: "codex.exe")
    cmd = launcher.build_apply_agent_command(
        agent="codex",
        model=None,
        mcp_config_path=tmp_path / ".mcp-apply-0.json",
        cdp_port=9333,
    )

    assert cmd[:2] == ["codex.exe", "exec"]
    assert "--model" not in cmd


def test_build_apply_agent_command_drops_claude_tier_model_for_codex(monkeypatch, tmp_path: Path) -> None:
    """Regression: the fleet/CLI default --model is 'sonnet' (a Claude tier). Passing it
    to `codex exec --model sonnet` fails the turn ("model not supported") so Codex never
    prints a RESULT: line -> no_result_line. Codex must fall back to its own default."""
    from applypilot.apply import launcher

    monkeypatch.setattr(launcher.config, "get_codex_path", lambda: "codex.exe")
    for tier in ("sonnet", "Opus", "haiku"):
        cmd = launcher.build_apply_agent_command(
            agent="codex",
            model=tier,
            mcp_config_path=tmp_path / ".mcp-apply-0.json",
            cdp_port=9333,
        )
        assert "--model" not in cmd, f"codex must not receive Claude tier {tier!r}"
        assert tier.lower() not in [c.lower() for c in cmd]

    # A genuine Codex model is still forwarded.
    cmd = launcher.build_apply_agent_command(
        agent="codex",
        model="gpt-5-codex",
        mcp_config_path=tmp_path / ".mcp-apply-0.json",
        cdp_port=9333,
    )
    assert cmd[:4] == ["codex.exe", "exec", "--model", "gpt-5-codex"]

    # The canary path shares the same guard.
    canary = launcher.build_agent_canary_command("codex", "sonnet")
    assert "--model" not in canary


def test_build_apply_agent_canary_command_uses_selected_agent(monkeypatch) -> None:
    from applypilot.apply import launcher

    monkeypatch.setattr(launcher.config, "get_claude_path", lambda: "claude.exe")
    monkeypatch.setattr(launcher.config, "get_codex_path", lambda: "codex.exe")

    assert launcher.build_agent_canary_command("claude", "sonnet")[:3] == [
        "claude.exe",
        "--model",
        "sonnet",
    ]

    codex_cmd = launcher.build_agent_canary_command("codex", None)
    assert codex_cmd[:2] == ["codex.exe", "exec"]
    assert "--model" not in codex_cmd
    assert "--ignore-user-config" in codex_cmd
    assert {"plugins", "apps", "memories"}.issubset(set(codex_cmd))
    assert "Reply with the single word READY." in codex_cmd


def test_worker_loop_passes_selected_agent_to_run_job(monkeypatch) -> None:
    from applypilot.apply import launcher

    launcher._stop_event.clear()
    captured: dict = {}
    job = {
        "url": "https://example.com/job",
        "application_url": "https://example.com/apply",
        "title": "Chief of Staff",
        "site": "ExampleCo",
    }

    monkeypatch.setattr(launcher, "update_state", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "add_event", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "get_state", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "launch_chrome", lambda *a, **k: object())
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "acquire_job", lambda **kwargs: job)
    monkeypatch.setattr(launcher, "mark_result", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "_throttle_before_apply", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "_throttle_after_apply", lambda *a, **k: None)

    def fake_run_job(*args, **kwargs):
        captured.update(kwargs)
        return "applied", 100

    monkeypatch.setattr(launcher, "run_job", fake_run_job)

    applied, failed = launcher.worker_loop(
        worker_id=0,
        limit=1,
        min_score=7,
        dry_run=False,
        agent="codex",
        model="gpt-5",
    )

    assert applied == 1 and failed == 0
    assert captured["agent"] == "codex"
    assert captured["model"] == "gpt-5"
