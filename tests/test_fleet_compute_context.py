import json
from applypilot.apply import pgqueue
from applypilot.fleet import compute_context as cc


def test_publish_then_load_roundtrip(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        cc.publish_context(conn, resume_text="RESUME", preference_profile={"weights": 1},
                           kg_prompt="KG", search_cfg={"score_audit": {}}, version="v1")
        ctx, version = cc.load_context(conn, providers=["deepseek"], fallback=["gemini"], ensemble=True)
    assert version == "v1"
    assert ctx.resume_text == "RESUME" and ctx.kg_prompt == "KG"
    assert ctx.preference_profile == {"weights": 1} and ctx.search_cfg == {"score_audit": {}}
    assert ctx.providers == ["deepseek"] and ctx.fallback == ["gemini"] and ctx.ensemble is True


def test_load_missing_context_returns_empty_version(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        ctx, version = cc.load_context(conn, providers=["deepseek"])
    assert version == "" and ctx.resume_text == ""
