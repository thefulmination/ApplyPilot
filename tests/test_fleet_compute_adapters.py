from applypilot.fleet import compute_adapters as ca


def _ctx(**kw):
    base = dict(resume_text="RESUME", preference_profile=None, kg_prompt=None,
                search_cfg=None, providers=["deepseek"], fallback=[], ensemble=False)
    base.update(kw); return ca.ComputeContext(**base)


def test_score_fn_maps_payload_and_captures_cost(monkeypatch):
    calls = {}
    def fake_score_job(resume, job, preference_profile=None, knowledge_graph_prompt=None, provider=None):
        calls["job"] = job; calls["provider"] = provider
        return {"score": 9, "keywords": "ops", "reasoning": "strong", "model": "deepseek-v4-flash", "provider": "deepseek"}
    class FakeClient:
        model = "deepseek-v4-flash"; last_usage = {"prompt_tokens": 100, "completion_tokens": 20}
    monkeypatch.setattr(ca, "score_job", fake_score_job)
    monkeypatch.setattr(ca, "get_client", lambda **k: FakeClient())
    monkeypatch.setattr(ca, "estimate_cost", lambda m, u: 0.0004)

    payload = {"url": "u1", "company": "Acme", "title": "Chief of Staff",
               "application_url": "https://x", "full_description": "ops role"}
    score_fn = ca.make_score_fn(_ctx())
    result, cost = score_fn(payload)
    assert calls["job"]["title"] == "Chief of Staff" and calls["job"]["site"] == "Acme"
    assert calls["job"]["full_description"] == "ops role" and calls["provider"] == "deepseek"
    assert result["research_fit_score"] == 9 and result["status"] == "done"
    assert result["provider"] == "deepseek" and result["model"] == "deepseek-v4-flash"
    assert cost == 0.0004


def test_score_fn_maps_llm_error_to_failed(monkeypatch):
    monkeypatch.setattr(ca, "score_job", lambda *a, **k: {"score": 0, "keywords": "", "reasoning": "x", "error": "429"})
    monkeypatch.setattr(ca, "get_client", lambda **k: type("C", (), {"model": "m", "last_usage": None})())
    monkeypatch.setattr(ca, "estimate_cost", lambda m, u: None)
    score_fn = ca.make_score_fn(_ctx())
    result, cost = score_fn({"url": "u", "company": "C", "title": "T", "full_description": "d"})
    assert result["status"] == "failed" and result["research_fit_score"] is None
    assert cost == 0.0
