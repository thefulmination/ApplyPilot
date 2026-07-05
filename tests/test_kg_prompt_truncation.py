import json
import logging

import pytest

from applypilot import database
from applypilot.scoring import scorer


def test_knowledge_graph_loader_keeps_under_cap_file(tmp_path, monkeypatch):
    kg = tmp_path / "kg.md"
    kg.write_text("KG PACK\n", encoding="utf-8")
    monkeypatch.setattr(scorer, "KNOWLEDGE_GRAPH_PROMPT_PATH", kg)
    monkeypatch.setattr(scorer, "_MAX_KNOWLEDGE_GRAPH_CHARS", 32000)

    assert scorer._load_knowledge_graph_prompt() == "KG PACK\n"


def test_knowledge_graph_loader_logs_and_records_over_cap_file(tmp_path, monkeypatch, caplog):
    kg = tmp_path / "kg.md"
    kg.write_text("A" * 12, encoding="utf-8")
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(scorer, "KNOWLEDGE_GRAPH_PROMPT_PATH", kg)
    monkeypatch.setattr(scorer, "_MAX_KNOWLEDGE_GRAPH_CHARS", 5)
    monkeypatch.setattr(scorer.database, "record_scoring_context_event",
                        lambda event, detail: events.append((event, detail)))

    with caplog.at_level(logging.ERROR, logger=scorer.log.name):
        loaded = scorer._load_knowledge_graph_prompt()

    assert loaded == "A" * 5 + "\n...[truncated]"
    assert any("Knowledge graph prompt" in r.message and "truncating" in r.message
               for r in caplog.records)
    assert events and events[0][0] == "kg_prompt_truncated"
    detail = json.loads(events[0][1])
    assert detail == {"path": str(kg), "original_chars": 12, "cap": 5}


def test_score_context_loaders_share_knowledge_graph_helper(monkeypatch, tmp_path):
    resume = tmp_path / "resume.txt"
    resume.write_text("RESUME", encoding="utf-8")
    calls = []
    monkeypatch.setattr(scorer, "RESUME_PATH", resume)
    monkeypatch.setattr(scorer, "load_preference_profile", lambda: None)
    monkeypatch.setattr(scorer, "_load_knowledge_graph_prompt",
                        lambda: calls.append("kg") or "KG")

    assert scorer.load_score_context()["kg_prompt"] == "KG"

    class StopAfterContext(Exception):
        pass

    def stop_before_db():
        raise StopAfterContext()

    monkeypatch.setattr(scorer, "get_connection", stop_before_db)
    with pytest.raises(StopAfterContext):
        scorer.run_scoring(limit=1)

    assert calls == ["kg", "kg"]


def test_record_scoring_context_event_is_persisted_and_best_effort(tmp_path):
    db = tmp_path / "events.db"
    conn = database.init_db(db)

    database.record_scoring_context_event("kg_prompt_truncated", '{"cap":5}', conn=conn)

    row = conn.execute(
        "SELECT event, detail FROM scoring_context_events"
    ).fetchone()
    assert tuple(row) == ("kg_prompt_truncated", '{"cap":5}')
    database.record_scoring_context_event("ignored", "{}", conn=object())
