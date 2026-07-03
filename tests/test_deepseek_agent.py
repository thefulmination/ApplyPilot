"""DeepSeek as a third apply agent: run via the Codex CLI runtime (which drives the
Playwright MCP browser) pointed at DeepSeek's OpenAI-compatible API through a Codex custom
model_provider -- no LiteLLM proxy needed, just DEEPSEEK_API_KEY. Command-builder tests are
pure (no spend / no subprocess)."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_normalize_agent_accepts_deepseek():
    from applypilot.apply import launcher
    assert launcher._normalize_agent("deepseek") == "deepseek"
    assert launcher._normalize_agent("DeepSeek") == "deepseek"


def test_normalize_agent_still_rejects_unknown():
    from applypilot.apply import launcher
    with pytest.raises(ValueError):
        launcher._normalize_agent("gemini")


def test_deepseek_uses_codex_runtime_with_provider(monkeypatch, tmp_path: Path):
    from applypilot.apply import launcher
    monkeypatch.setattr(launcher.config, "get_codex_path", lambda: "codex.exe")
    monkeypatch.delenv("APPLYPILOT_DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("APPLYPILOT_DEEPSEEK_BASE_URL", raising=False)

    cmd = launcher.build_apply_agent_command(
        agent="deepseek", model="sonnet",
        mcp_config_path=tmp_path / ".mcp-apply-0.json", cdp_port=9444,
    )

    # Codex runtime (drives the browser); default DeepSeek model, not the Claude tier.
    assert cmd[:2] == ["codex.exe", "exec"]
    assert "--model" in cmd and "deepseek-chat" in cmd
    assert "sonnet" not in cmd
    # DeepSeek provider wired via -c overrides.
    assert 'model_provider="deepseek"' in cmd
    assert any(c.startswith("model_providers.deepseek.base_url=") for c in cmd)
    assert any("https://api.deepseek.com" in c for c in cmd)
    assert any('model_providers.deepseek.env_key="DEEPSEEK_API_KEY"' == c for c in cmd)
    # Still drives the Playwright MCP browser + stays sandboxed, like the codex path.
    assert any("mcp_servers.playwright.command" in c for c in cmd)
    assert any(f"--cdp-endpoint=http://localhost:9444" in c for c in cmd)
    assert "--sandbox" in cmd and "read-only" in cmd
    assert cmd[-1] == "-"


def test_deepseek_model_and_base_url_are_env_configurable(monkeypatch, tmp_path: Path):
    from applypilot.apply import launcher
    monkeypatch.setattr(launcher.config, "get_codex_path", lambda: "codex.exe")
    monkeypatch.setenv("APPLYPILOT_DEEPSEEK_MODEL", "deepseek-reasoner")
    monkeypatch.setenv("APPLYPILOT_DEEPSEEK_BASE_URL", "https://proxy.internal/v1")

    cmd = launcher.build_apply_agent_command(
        agent="deepseek", model="sonnet",
        mcp_config_path=tmp_path / ".mcp-apply-0.json", cdp_port=9444,
    )
    assert "deepseek-reasoner" in cmd
    assert any("https://proxy.internal/v1" in c for c in cmd)


def test_deepseek_canary_uses_codex_runtime(monkeypatch):
    from applypilot.apply import launcher
    monkeypatch.setattr(launcher.config, "get_codex_path", lambda: "codex.exe")
    monkeypatch.delenv("APPLYPILOT_DEEPSEEK_MODEL", raising=False)
    canary = launcher.build_agent_canary_command("deepseek", "sonnet")
    assert canary[:2] == ["codex.exe", "exec"]
    assert "deepseek-chat" in canary
    assert 'model_provider="deepseek"' in canary
    assert "Reply with the single word READY." in canary
