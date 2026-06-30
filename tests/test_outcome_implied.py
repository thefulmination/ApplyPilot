from applypilot.outcome_implied import implied_status


def _row(**kw):
    base = {"job_url": "https://j/1", "current_stage": "applied", "outcome": None, "events": []}
    base.update(kw); return base


def test_acknowledged_only_implies_nothing():
    r = _row(current_stage="acknowledged", events=[
        {"message_id": "m1", "occurred_at": "2026-06-02T00:00:00+00:00", "stage": "acknowledged", "outcome": None, "confidence": "low"},
    ])
    assert implied_status(r) is None


def test_offer_implies_offer_with_source_event():
    r = _row(current_stage="offer", outcome="offer", events=[
        {"message_id": "a", "occurred_at": "2026-06-02T00:00:00+00:00", "stage": "acknowledged", "outcome": None, "confidence": "low"},
        {"message_id": "o", "occurred_at": "2026-06-16T00:00:00+00:00", "stage": "offer", "outcome": "offer", "confidence": "high"},
    ])
    out = implied_status(r)
    assert out["implied_status"] == "offer"
    assert out["source_message_id"] == "o"
    assert out["occurred_at"] == "2026-06-16T00:00:00+00:00"
    assert out["job_url"] == "https://j/1"


def test_rejected_implies_rejected():
    r = _row(current_stage="rejected", outcome="rejected", events=[
        {"message_id": "x", "occurred_at": "2026-06-10T00:00:00+00:00", "stage": "rejected", "outcome": "rejected", "confidence": "high"},
    ])
    assert implied_status(r)["implied_status"] == "rejected"


def test_interview_stage_implies_recruiter_screen_from_first_response():
    r = _row(current_stage="interview", outcome=None, events=[
        {"message_id": "ack", "occurred_at": "2026-06-02T00:00:00+00:00", "stage": "acknowledged", "outcome": None, "confidence": "low"},
        {"message_id": "scr", "occurred_at": "2026-06-05T00:00:00+00:00", "stage": "screen", "outcome": None, "confidence": "medium"},
        {"message_id": "iv", "occurred_at": "2026-06-12T00:00:00+00:00", "stage": "interview", "outcome": None, "confidence": "high"},
    ])
    out = implied_status(r)
    assert out["implied_status"] == "recruiter_screen"
    assert out["source_message_id"] == "scr"   # first RESPONSE-stage event


def test_other_or_applied_implies_nothing():
    assert implied_status(_row(current_stage="applied")) is None
    assert implied_status(_row(current_stage="other", events=[
        {"message_id": "z", "occurred_at": "2026-06-02T00:00:00+00:00", "stage": "other", "outcome": None, "confidence": "low"},
    ])) is None
