from applypilot.scoring import scorer


def test_build_score_prompt_text_includes_resume_job_and_instructions():
    job = {"title": "Chief of Staff", "site": "Acme", "location": "Remote",
           "full_description": "Lead operations and strategy."}
    text = scorer.build_score_prompt_text("RESUME-OPS-LEADER", job, knowledge_graph_prompt="KG-PACK")
    assert isinstance(text, str)
    assert "RESUME-OPS-LEADER" in text
    assert "Chief of Staff" in text and "Acme" in text and "Lead operations" in text
    assert "KG-PACK" in text
    # carries the scoring instruction (the SCORE_PROMPT system text)
    assert "fit" in text.lower() and "score" in text.lower()


def test_load_score_context(monkeypatch, tmp_path):
    r = tmp_path / "resume.txt"; r.write_text("RESUME", encoding="utf-8")
    k = tmp_path / "kg.txt"; k.write_text("KG", encoding="utf-8")
    monkeypatch.setattr(scorer, "RESUME_PATH", r)
    monkeypatch.setattr(scorer, "KNOWLEDGE_GRAPH_PROMPT_PATH", k)
    monkeypatch.setattr(scorer, "load_preference_profile", lambda: {"summary": "ops"})
    ctx = scorer.load_score_context()
    assert ctx["resume_text"] == "RESUME" and ctx["kg_prompt"] == "KG"
    assert ctx["preference_profile"] == {"summary": "ops"}
