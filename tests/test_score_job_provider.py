from applypilot import scoring
from applypilot.scoring import scorer


def test_score_job_forwards_provider_override(monkeypatch):
    seen = {}

    class FakeClient:
        model = "gemini-2.0-flash"
        provider_name = "gemini"
        def chat(self, *a, **k):
            return "SCORE: 8\nKEYWORDS: ops\nVERDICT: Good fit - test\nREASONING: test reasoning"

    def fake_get_client(model_override=None, stage=None, provider_override=None):
        seen["stage"] = stage
        seen["provider_override"] = provider_override
        return FakeClient()

    monkeypatch.setattr(scorer, "get_client", fake_get_client)
    job = {"title": "Chief of Staff", "site": "Acme", "location": "Remote", "full_description": "ops"}
    out = scorer.score_job("RESUME", job, provider="gemini")
    assert seen["stage"] == "score" and seen["provider_override"] == "gemini"
    assert out["score"] == 8


def test_score_job_uses_company_with_site_fallback(monkeypatch):
    prompts = []

    class FakeClient:
        model = "gemini-2.0-flash"
        provider_name = "gemini"

        def chat(self, *a, **k):
            prompts.append(a[0][1]["content"])
            return "SCORE: 8\nKEYWORDS: ops\nVERDICT: Good fit - test\nREASONING: test reasoning"

    monkeypatch.setattr(scorer, "get_client", lambda **_kwargs: FakeClient())

    scorer.score_job("RESUME", {
        "title": "Chief of Staff", "site": "linkedin", "company": "RealCo",
        "location": "Remote", "full_description": "x" * 600,
    })
    scorer.score_job("RESUME", {
        "title": "Chief of Staff", "site": "FallbackSite",
        "location": "Remote", "full_description": "x" * 600,
    })

    assert "COMPANY: RealCo" in prompts[0]
    assert "COMPANY: linkedin" not in prompts[0]
    assert "COMPANY: FallbackSite" in prompts[1]
