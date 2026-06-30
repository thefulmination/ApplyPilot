from applypilot.outcome_timeline import build_timeline


def _ev(occurred_at, stage, outcome=None):
    return {"occurred_at": occurred_at, "stage": stage, "outcome": outcome}


def test_empty_events_is_silent():
    t = build_timeline("2026-06-01T00:00:00+00:00", [], now_iso="2026-06-20T00:00:00+00:00")
    assert t["responded"] is False
    assert t["current_stage"] == "applied"
    assert t["outcome"] is None
    assert t["silent_days"] == 19
    assert t["first_response_days"] is None


def test_acknowledged_only_is_not_a_response():
    t = build_timeline("2026-06-01T00:00:00+00:00",
                       [_ev("2026-06-02T00:00:00+00:00", "acknowledged")],
                       now_iso="2026-06-10T00:00:00+00:00")
    assert t["responded"] is False
    assert t["current_stage"] == "acknowledged"


def test_full_path_to_offer_computes_latencies():
    events = [
        _ev("2026-06-02T00:00:00+00:00", "acknowledged"),
        _ev("2026-06-05T00:00:00+00:00", "screen"),
        _ev("2026-06-12T00:00:00+00:00", "interview"),
        _ev("2026-06-16T00:00:00+00:00", "offer", outcome="offer"),
    ]
    t = build_timeline("2026-06-01T00:00:00+00:00", events, now_iso="2026-06-20T00:00:00+00:00")
    assert t["responded"] is True
    assert t["positive"] is True
    assert t["outcome"] == "offer"
    assert t["decision_stage"] == "offer"
    assert t["first_response_days"] == 4   # applied 06-01 -> first non-ack (screen) 06-05
    assert t["decision_days"] == 15        # applied 06-01 -> offer 06-16
    assert t["ordered"][0]["stage"] == "acknowledged"


def test_events_are_sorted_by_time():
    events = [
        _ev("2026-06-16T00:00:00+00:00", "rejected", outcome="rejected"),
        _ev("2026-06-02T00:00:00+00:00", "acknowledged"),
    ]
    t = build_timeline("2026-06-01T00:00:00+00:00", events, now_iso="2026-06-20T00:00:00+00:00")
    assert [e["stage"] for e in t["ordered"]] == ["acknowledged", "rejected"]
    assert t["outcome"] == "rejected"
    assert t["decision_stage"] == "rejected"
