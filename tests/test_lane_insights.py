from applypilot.lane_insights import compute_lane_insights, derive_segments, wilson_interval


def test_derive_segments_buckets_score_and_title():
    seg = derive_segments({
        "source_board": "greenhouse", "title": "Senior Quant Analyst",
        "fit_score": 8, "audit_score": None, "location": "Remote",
        "salary": "$200,000", "fit_gap_category": "stretch",
    })
    assert seg["source_board"] == "greenhouse"
    assert seg["score_band"] == "8+"
    assert seg["seniority"] == "senior"
    assert seg["role_family"] == "quant"
    assert seg["location_bucket"] == "remote"


def test_wilson_interval_widens_for_small_n():
    lo_small, hi_small = wilson_interval(1, 2)
    lo_big, hi_big = wilson_interval(50, 100)
    assert (hi_small - lo_small) > (hi_big - lo_big)


def test_thin_segment_is_insufficient_not_flagged():
    apps = [{"responded": True, "positive": True, "segments": {"source_board": "tinyboard"}}]
    apps += [{"responded": False, "positive": False, "segments": {"source_board": "bulk"}} for _ in range(20)]
    out = compute_lane_insights(apps, floor=8)
    tiny = next(s for s in out["segments"] if s["value"] == "tinyboard")
    assert tiny["flag"] == "insufficient"


def test_strong_segment_flags_warm():
    # 12 responders on "greatboard", 0 elsewhere -> greatboard clearly above baseline.
    apps = [{"responded": True, "positive": True, "segments": {"source_board": "greatboard"}} for _ in range(12)]
    apps += [{"responded": False, "positive": False, "segments": {"source_board": "coldboard"}} for _ in range(40)]
    out = compute_lane_insights(apps, floor=8)
    great = next(s for s in out["segments"] if s["value"] == "greatboard")
    cold = next(s for s in out["segments"] if s["value"] == "coldboard")
    assert great["flag"] == "warm"
    assert cold["flag"] == "cold"
