from __future__ import annotations

from applypilot.scoring import scorer


def test_rescore_jobs_command_calls_scoring_in_rescore_mode(monkeypatch) -> None:
    from typer.testing import CliRunner

    from applypilot import cli, config

    called: dict[str, object] = {}

    def fake_run_scoring(limit: int = 0, rescore: bool = False) -> dict:
        called["limit"] = limit
        called["rescore"] = rescore
        return {"scored": 3, "errors": 0, "elapsed": 1.2, "distribution": [(9, 2), (8, 1)]}

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(config, "check_tier", lambda _tier, _feature: None)
    monkeypatch.setattr(scorer, "run_scoring", fake_run_scoring)

    result = CliRunner().invoke(cli.app, ["rescore-jobs", "--limit", "25"])

    assert result.exit_code == 0
    assert called == {"limit": 25, "rescore": True}
    assert "Jobs rescored: 3" in result.output


def test_load_preference_profile_uses_env_path_at_call_time(tmp_path, monkeypatch) -> None:
    from applypilot import config

    profile_path = tmp_path / "preference-profile.json"
    profile_path.write_text('{"summary": {"reviewedJobs": 3}}', encoding="utf-8")
    monkeypatch.setenv("APPLYPILOT_PREFERENCE_PROFILE_PATH", str(profile_path))

    profile = config.load_preference_profile()

    assert profile == {"summary": {"reviewedJobs": 3}}


def test_score_job_includes_preference_profile_in_llm_prompt(monkeypatch) -> None:
    captured_messages: list[dict] = []

    class FakeClient:
        model = "test-model"
        provider_name = "test-provider"

        def chat(self, messages: list[dict], max_tokens: int, temperature: float) -> str:
            captured_messages.extend(messages)
            return "SCORE: 8\nKEYWORDS: chief of staff, partnerships\nREASONING: Strong preference fit."

    monkeypatch.setattr(scorer, "get_client", lambda stage: FakeClient())

    result = scorer.score_job(
        "Resume text",
        {
            "title": "Chief of Staff",
            "site": "ExampleCo",
            "location": "New York, NY",
            "full_description": "Own executive cadence and strategic partnerships.",
        },
        preference_profile={
            "promptSummary": "Boost partnerships-fit. Penalize clinical-license requirements.",
        },
    )

    assert result["score"] == 8
    prompt = "\n\n".join(message["content"] for message in captured_messages)
    assert "HUMAN JOB PREFERENCE PROFILE" in prompt
    assert "Boost partnerships-fit" in prompt
    assert "Penalize clinical-license" in prompt
