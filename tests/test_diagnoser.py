from pathlib import Path
from applypilot.fleet import diagnoser
from applypilot.fleet.diagnoser import WorkerCtx

FIX = Path(__file__).parent / "fixtures" / "diagnoser"

def test_tier0_detects_usage_limit_with_model_and_reset():
    log = (FIX / "usage_limit.log").read_text(encoding="utf-8")
    d = diagnoser.tier0_diagnose(WorkerCtx(worker_id="m2-3", recent_log=log))
    assert d is not None
    assert d.root_cause == "usage_limit"
    assert d.source == "tier0"
    assert d.confidence == 1.0
    assert d.details["reset_at"] == "8:10 PM"
    assert "GPT-5.3-Codex-Spark" in d.details["model"]
    assert "RE-QUEUE" in d.recommendation.upper()

def test_tier0_returns_none_when_no_usage_limit():
    d = diagnoser.tier0_diagnose(WorkerCtx(worker_id="m2-3", recent_log="filling the form, clicked submit"))
    assert d is None
