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


def test_load_preference_profile_tolerates_malformed_json(tmp_path, monkeypatch) -> None:
    # A bad file from the external recommendation engine must not crash scoring.
    from applypilot import config

    bad = tmp_path / "preference-profile.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("APPLYPILOT_PREFERENCE_PROFILE_PATH", str(bad))

    assert config.load_preference_profile() is None


def test_load_preference_profile_rejects_non_object(tmp_path, monkeypatch) -> None:
    from applypilot import config

    arr = tmp_path / "preference-profile.json"
    arr.write_text('["not", "an", "object"]', encoding="utf-8")
    monkeypatch.setenv("APPLYPILOT_PREFERENCE_PROFILE_PATH", str(arr))

    assert config.load_preference_profile() is None


def test_preference_profile_prompt_tolerates_wrong_types() -> None:
    # Signals arriving as the wrong type (dict/str/None instead of list) must
    # not crash the renderer.
    from applypilot.scoring import scorer

    profile = {
        "positiveSignals": {"oops": "dict not list"},
        "negativeSignals": "string not list",
        "fitMapRules": None,
        "summary": {"reviewedJobs": 5},
    }
    text = scorer._preference_profile_prompt(profile)
    assert "HUMAN JOB PREFERENCE PROFILE" in text  # rendered, did not raise


def test_preference_profile_prompt_truncates_large_input() -> None:
    from applypilot.scoring import scorer

    text = scorer._preference_profile_prompt({"promptSummary": "x" * 50000})
    assert "[truncated]" in text
    assert len(text) < scorer._MAX_PREFERENCE_CHARS + 200


def test_score_job_survives_malformed_preference_profile(monkeypatch) -> None:
    from applypilot.scoring import scorer

    class FakeClient:
        model = "m"
        provider_name = "p"

        def chat(self, messages, max_tokens, temperature, stage=None):
            return "SCORE: 6\nKEYWORDS: x\nREASONING: ok"

    monkeypatch.setattr(scorer, "get_client", lambda stage: FakeClient())

    result = scorer.score_job(
        "Resume",
        {"title": "Analyst", "site": "Co", "location": "Remote", "full_description": "desc"},
        preference_profile={"positiveSignals": "not-a-list", "negativeSignals": 123},
    )
    assert result["score"] == 6


def test_score_job_includes_preference_profile_in_llm_prompt(monkeypatch) -> None:
    captured_messages: list[dict] = []

    class FakeClient:
        model = "test-model"
        provider_name = "test-provider"

        def chat(self, messages: list[dict], max_tokens: int, temperature: float, stage: str | None = None) -> str:
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


# antigravity: test-preference-profile-scoring-kg-1
def test_score_job_includes_knowledge_graph_in_llm_prompt(monkeypatch) -> None:
    captured_messages: list[dict] = []

    class FakeClient:
        model = "test-model"
        provider_name = "test-provider"

        def chat(self, messages: list[dict], max_tokens: int, temperature: float, stage: str | None = None) -> str:
            captured_messages.extend(messages)
            return "SCORE: 7\nKEYWORDS: quantitative finance\nREASONING: Factual match citing education:stevens-quantitative-finance-bs."

    monkeypatch.setattr(scorer, "get_client", lambda stage: FakeClient())

    result = scorer.score_job(
        "Resume text",
        {
            "title": "Quantitative Analyst",
            "site": "QuantCo",
            "location": "Remote",
            "full_description": "We need quantitative finance experience.",
        },
        preference_profile=None,
        knowledge_graph_prompt="APPLYPILOT KNOWLEDGE GRAPH\nMock Graph content here.",
    )

    assert result["score"] == 7
    system_msg = next(m["content"] for m in captured_messages if m["role"] == "system")
    user_msg = next(m["content"] for m in captured_messages if m["role"] == "user")

    assert "KNOWLEDGE GRAPH INSTRUCTIONS:" in system_msg
    assert "Cite specific graph node IDs" in system_msg
    assert "APPLYPILOT KNOWLEDGE GRAPH" in user_msg
    assert "Mock Graph content here." in user_msg

