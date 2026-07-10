from __future__ import annotations

import io
import json
import sys
import threading
import types
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clear_codex_apply_overrides(monkeypatch) -> None:
    monkeypatch.delenv("APPLYPILOT_CODEX_MODEL", raising=False)
    monkeypatch.delenv("APPLYPILOT_CODEX_REASONING_EFFORT", raising=False)


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


def test_codex_model_override_keeps_claude_model_separate(monkeypatch, tmp_path: Path) -> None:
    from applypilot.apply import launcher

    monkeypatch.setattr(launcher.config, "get_codex_path", lambda: "codex.exe")
    monkeypatch.setenv("APPLYPILOT_CODEX_MODEL", "gpt-5.4-mini")
    monkeypatch.setenv("APPLYPILOT_CODEX_REASONING_EFFORT", "medium")

    cmd = launcher.build_apply_agent_command(
        agent="codex",
        model="sonnet",
        mcp_config_path=tmp_path / ".mcp-apply-0.json",
        cdp_port=9333,
    )

    assert cmd[:4] == ["codex.exe", "exec", "--model", "gpt-5.4-mini"]
    assert "-c" in cmd
    assert 'model_reasoning_effort="medium"' in cmd
    assert "sonnet" not in cmd

    canary = launcher.build_agent_canary_command("codex", "sonnet")
    assert canary[:4] == ["codex.exe", "exec", "--model", "gpt-5.4-mini"]
    assert 'model_reasoning_effort="medium"' in canary


def test_codex_reasoning_effort_rejects_invalid_value(monkeypatch, tmp_path: Path) -> None:
    from applypilot.apply import launcher

    monkeypatch.setattr(launcher.config, "get_codex_path", lambda: "codex.exe")
    monkeypatch.setenv("APPLYPILOT_CODEX_REASONING_EFFORT", "hot")

    with pytest.raises(ValueError, match="APPLYPILOT_CODEX_REASONING_EFFORT"):
        launcher.build_apply_agent_command(
            agent="codex",
            model="gpt-5.4-mini",
            mcp_config_path=tmp_path / ".mcp-apply-0.json",
            cdp_port=9333,
        )


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
    monkeypatch.setenv("APPLYPILOT_PREFLIGHT_LIVENESS", "0")

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


def test_route_from_greenhouse_result_names_shadow_and_submit():
    from applypilot.apply import launcher

    assert launcher._route_from_greenhouse_result(
        {"route": "deterministic", "ready": True},
        own=False,
    ) == "adapter_shadow:greenhouse"
    assert launcher._route_from_greenhouse_result(
        {"route": "deterministic", "ready": True},
        own=True,
    ) == "adapter_submit:greenhouse"
    assert launcher._route_from_greenhouse_result(
        {"route": "agent_fallback", "ready": False},
        own=False,
    ) == "agent"


def test_greenhouse_shadow_result_records_adapter_route_stats(monkeypatch, tmp_path: Path):
    from applypilot.apply import greenhouse_adapter, greenhouse_submit, launcher

    worker_id = 91
    launcher._adapter_route_stats.clear()
    monkeypatch.setattr(greenhouse_submit, "adapter_enabled", lambda: True)
    monkeypatch.setattr(greenhouse_submit, "submit_enabled", lambda: False)
    monkeypatch.setattr(greenhouse_adapter, "parse_greenhouse_url", lambda url: ("acme", "123"))
    monkeypatch.setattr(launcher.config, "load_profile", lambda: {"personal": {}})

    monkeypatch.setattr(
        greenhouse_submit,
        "apply_greenhouse",
        lambda *args, **kwargs: {
            "route": "deterministic",
            "ready": True,
            "plan": types.SimpleNamespace(free_text={}),
        },
    )

    class FakePage:
        def goto(self, *args, **kwargs):
            return None

        def close(self):
            return None

    class FakeContext:
        def new_page(self):
            return FakePage()

    class FakeBrowser:
        contexts = [FakeContext()]

    class FakeChromium:
        def connect_over_cdp(self, endpoint: str):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightContext:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, exc_type, exc, tb):
            return False

    playwright_module = types.ModuleType("playwright")
    sync_api_module = types.ModuleType("playwright.sync_api")
    sync_api_module.sync_playwright = lambda: FakePlaywrightContext()
    playwright_module.sync_api = sync_api_module
    monkeypatch.setitem(sys.modules, "playwright", playwright_module)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api_module)

    result = launcher._maybe_greenhouse_apply(
        {
            "application_url": "https://boards.greenhouse.io/acme/jobs/123",
            "url": "https://boards.greenhouse.io/acme/jobs/123",
        },
        9222,
        dry_run=False,
        resume_text="resume",
        resume_path=tmp_path / "resume.txt",
        worker_id=worker_id,
    )

    assert result is None
    assert launcher._adapter_route_stats.pop(worker_id) == {
        "route": "adapter_shadow:greenhouse",
        "adapter_name": "greenhouse",
        "adapter_plan_ready": True,
    }


def test_greenhouse_owned_submit_uses_attempt_store_and_records_verifier(monkeypatch, tmp_path: Path):
    from applypilot.apply import greenhouse_adapter, greenhouse_submit, launcher

    worker_id = 93
    launcher._adapter_route_stats.clear()
    monkeypatch.setattr(greenhouse_submit, "adapter_enabled", lambda: True)
    monkeypatch.setattr(greenhouse_submit, "submit_enabled", lambda: True)
    monkeypatch.setattr(greenhouse_adapter, "parse_greenhouse_url", lambda url: ("acme", "123"))
    monkeypatch.setattr(launcher.config, "load_profile", lambda: {"personal": {}})

    class FakeStore:
        def __init__(self):
            self.calls = []

        def create_prepared(self, **kwargs):
            self.calls.append(("create_prepared", kwargs))
            return "attempt-1"

        def transition(self, attempt_id, **kwargs):
            self.calls.append(("transition", attempt_id, kwargs))
            return {"state": kwargs["state"]}

    store = FakeStore()

    def fake_apply(*args, **kwargs):
        context = kwargs["on_plan_ready"](
            types.SimpleNamespace(ready=True, unmapped_required=[]),
            [types.SimpleNamespace(kind="fill"), types.SimpleNamespace(kind="submit")],
        )
        kwargs["before_submit"](context)
        return {
            "route": "deterministic",
            "ready": True,
            "status": "applied",
            "verification_status": "verified",
            "verification_method": "confirmation_dom",
            "verification_ref": "application submitted",
            "attempt_context": context,
        }

    monkeypatch.setattr(greenhouse_submit, "apply_greenhouse", fake_apply)

    class FakePage:
        def goto(self, *args, **kwargs):
            return None

        def close(self):
            return None

    class FakeContext:
        def new_page(self):
            return FakePage()

    class FakeBrowser:
        contexts = [FakeContext()]

    class FakeChromium:
        def connect_over_cdp(self, endpoint: str):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightContext:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, exc_type, exc, tb):
            return False

    playwright_module = types.ModuleType("playwright")
    sync_api_module = types.ModuleType("playwright.sync_api")
    sync_api_module.sync_playwright = lambda: FakePlaywrightContext()
    playwright_module.sync_api = sync_api_module
    monkeypatch.setitem(sys.modules, "playwright", playwright_module)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api_module)

    result = launcher._maybe_greenhouse_apply(
        {
            "application_url": "https://boards.greenhouse.io/acme/jobs/123",
            "url": "https://boards.greenhouse.io/acme/jobs/123",
        },
        9222,
        dry_run=False,
        resume_text="resume",
        resume_path=tmp_path / "resume.txt",
        worker_id=worker_id,
        attempt_store=store,
    )

    assert result[0] == "applied"
    assert [call[0] for call in store.calls] == [
        "create_prepared", "transition", "transition"
    ]
    assert store.calls[1][2]["state"] == "submit_started"
    assert store.calls[2][2]["state"] == "verified"
    stats = launcher._adapter_route_stats.pop(worker_id)
    assert stats["attempt_id"] == "attempt-1"
    assert stats["verification_method"] == "confirmation_dom"
    assert stats["submit_checkpoint_state"] == "verified"


def test_run_job_impl_merges_greenhouse_shadow_route_stats(monkeypatch, tmp_path: Path):
    from applypilot.apply import launcher

    worker_id = 92
    launcher._adapter_route_stats.clear()
    launcher._last_run_stats.pop(worker_id, None)
    monkeypatch.setattr(launcher.config, "resolve_resume_stem", lambda path: None)
    monkeypatch.setattr(launcher.config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda worker_id: tmp_path)
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", lambda **kwargs: "prompt")
    monkeypatch.setattr(launcher, "build_apply_agent_command", lambda **kwargs: ["agent"])
    monkeypatch.setattr(launcher, "_maybe_lever_shadow", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "get_state", lambda worker_id: None)

    def fake_greenhouse_apply(job, port, **kwargs):
        launcher._adapter_route_stats[kwargs["worker_id"]] = {
            "route": "adapter_shadow:greenhouse",
            "adapter_name": "greenhouse",
            "adapter_plan_ready": True,
        }
        return None

    monkeypatch.setattr(launcher, "_maybe_greenhouse_apply", fake_greenhouse_apply)

    class FakeStdin:
        def write(self, text: str):
            return len(text)

        def close(self):
            return None

    class FakePopen:
        def __init__(self, *args, **kwargs):
            self.stdin = FakeStdin()
            self.stdout = io.StringIO(
                json.dumps(
                    {
                        "type": "result",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                        "num_turns": 1,
                        "total_cost_usd": 0,
                        "result": "RESULT:APPLIED",
                    }
                )
                + "\n"
            )
            self.returncode = 0
            self.pid = 12345

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def poll(self):
            return self.returncode

    monkeypatch.setattr(launcher.subprocess, "Popen", FakePopen)

    status, _duration_ms = launcher._run_job_impl(
        {
            "application_url": "https://boards.greenhouse.io/acme/jobs/123",
            "url": "https://boards.greenhouse.io/acme/jobs/123",
            "title": "Software Engineer",
            "site": "Acme",
            "tailored_resume_path": None,
        },
        port=9222,
        worker_id=worker_id,
    )

    stats = launcher._last_run_stats[worker_id]
    assert status == "applied"
    assert stats["route"] == "adapter_shadow:greenhouse"
    assert stats["adapter_name"] == "greenhouse"
    assert stats["adapter_plan_ready"] is True
    assert worker_id not in launcher._adapter_route_stats


def test_terminal_result_survives_forced_stream_cleanup(monkeypatch, tmp_path: Path):
    from applypilot.apply import launcher

    worker_id = 93
    launcher._last_run_stats.pop(worker_id, None)
    monkeypatch.setenv("APPLYPILOT_TERMINAL_RESULT_GRACE_SECONDS", "0.01")
    monkeypatch.setattr(launcher.config, "resolve_resume_stem", lambda path: None)
    monkeypatch.setattr(launcher.config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda worker_id: tmp_path)
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", lambda **kwargs: "prompt")
    monkeypatch.setattr(launcher, "build_apply_agent_command", lambda **kwargs: ["agent"])
    monkeypatch.setattr(launcher, "_maybe_greenhouse_apply", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "_maybe_lever_shadow", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "get_state", lambda worker_id: None)

    class FakeStdin:
        def write(self, text: str):
            return len(text)

        def close(self):
            return None

    class HangingStdout:
        def __init__(self):
            self._sent = False
            self.release = threading.Event()

        def __iter__(self):
            return self

        def __next__(self):
            if not self._sent:
                self._sent = True
                return json.dumps(
                    {
                        "type": "result",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                        "num_turns": 1,
                        "total_cost_usd": 0,
                        "result": "RESULT:APPLIED",
                    }
                )
            self.release.wait(timeout=10)
            raise StopIteration

    process_holder = {}

    class FakePopen:
        def __init__(self, *args, **kwargs):
            self.stdin = FakeStdin()
            self.stdout = HangingStdout()
            self.returncode = None
            self.pid = 12346
            process_holder["proc"] = self

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    def kill_process_tree(_pid):
        proc = process_holder["proc"]
        proc.returncode = -9
        proc.stdout.release.set()

    monkeypatch.setattr(launcher.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(launcher, "_kill_process_tree", kill_process_tree)

    status, _duration_ms = launcher._run_job_impl(
        {
            "application_url": "https://jobs.example.com/123",
            "url": "https://jobs.example.com/123",
            "title": "Software Engineer",
            "site": "Acme",
            "tailored_resume_path": None,
        },
        port=9223,
        worker_id=worker_id,
    )

    stats = launcher._last_run_stats[worker_id]
    assert status == "applied"
    assert process_holder["proc"].returncode == -9
    assert stats["route"] == "agent"
    assert stats["tool_calls_total"] == 0
    assert stats["application_tool_calls"] == 0
    assert stats["job_log_path"]
    assert stats["transcript_digest"].startswith("sha256:")
    assert stats["final_result_source"] == "final_message"


def test_intermediate_result_text_cannot_force_applied(monkeypatch, tmp_path: Path):
    from applypilot.apply import launcher

    worker_id = 94
    launcher._last_run_stats.pop(worker_id, None)
    monkeypatch.setattr(launcher, "AGENT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(launcher.config, "resolve_resume_stem", lambda path: None)
    monkeypatch.setattr(launcher.config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda worker_id: tmp_path)
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", lambda **kwargs: "prompt")
    monkeypatch.setattr(launcher, "build_apply_agent_command", lambda **kwargs: ["agent"])
    monkeypatch.setattr(launcher, "_maybe_greenhouse_apply", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "_maybe_lever_shadow", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "get_state", lambda worker_id: None)

    class FakeStdin:
        def write(self, text: str):
            return len(text)

        def close(self):
            return None

    class HangingStdout:
        def __init__(self):
            self._sent = False
            self.release = threading.Event()

        def __iter__(self):
            return self

        def __next__(self):
            if not self._sent:
                self._sent = True
                return json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Page body contains RESULT:APPLIED",
                                }
                            ]
                        },
                    }
                )
            self.release.wait(timeout=10)
            raise StopIteration

    process_holder = {}

    class FakePopen:
        def __init__(self, *args, **kwargs):
            self.stdin = FakeStdin()
            self.stdout = HangingStdout()
            self.returncode = None
            self.pid = 12347
            process_holder["proc"] = self

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    def kill_process_tree(_pid):
        proc = process_holder["proc"]
        proc.returncode = -9
        proc.stdout.release.set()

    monkeypatch.setattr(launcher.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(launcher, "_kill_process_tree", kill_process_tree)

    status, _duration_ms = launcher._run_job_impl(
        {
            "application_url": "https://jobs.example.com/124",
            "url": "https://jobs.example.com/124",
            "title": "Software Engineer",
            "site": "Acme",
            "tailored_resume_path": None,
        },
        port=9224,
        worker_id=worker_id,
    )

    assert status == "failed:timeout"
    assert launcher._last_run_stats[worker_id]["route"] == "agent"


def test_raw_result_line_cannot_force_applied(monkeypatch, tmp_path: Path):
    from applypilot.apply import launcher

    worker_id = 95
    launcher._last_run_stats.pop(worker_id, None)
    monkeypatch.setattr(launcher.config, "resolve_resume_stem", lambda path: None)
    monkeypatch.setattr(launcher.config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda worker_id: tmp_path)
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", lambda **kwargs: "prompt")
    monkeypatch.setattr(launcher, "build_apply_agent_command", lambda **kwargs: ["agent"])
    monkeypatch.setattr(launcher, "_maybe_greenhouse_apply", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "_maybe_lever_shadow", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "get_state", lambda worker_id: None)

    class FakeStdin:
        def write(self, text: str):
            return len(text)

        def close(self):
            return None

    class FakePopen:
        def __init__(self, *args, **kwargs):
            self.stdin = FakeStdin()
            self.stdout = io.StringIO("RESULT:APPLIED\n")
            self.returncode = 0
            self.pid = 12348

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    monkeypatch.setattr(launcher.subprocess, "Popen", FakePopen)

    status, _duration_ms = launcher._run_job_impl(
        {
            "application_url": "https://jobs.example.com/125",
            "url": "https://jobs.example.com/125",
            "title": "Software Engineer",
            "site": "Acme",
            "tailored_resume_path": None,
        },
        port=9225,
        worker_id=worker_id,
    )

    assert status == "failed:no_result_line"
    assert launcher._last_run_stats[worker_id]["route"] == "agent"
