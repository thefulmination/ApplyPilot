from applypilot.outcome_lane_signal import annotate_job_signal, compute_lane_report


def _app(responded, positive, board, role):
    return {"responded": responded, "positive": positive,
            "segments": {"source_board": board, "role_family": role,
                         "seniority": "mid", "score_band": "7",
                         "fit_gap_category": "unknown", "location_bucket": "remote",
                         "salary_band": "unknown"}}


def test_compute_lane_report_splits_warm_and_cold():
    rows = [_app(True, True, "greatboard", "quant") for _ in range(12)]
    rows += [_app(False, False, "coldboard", "data") for _ in range(40)]
    rep = compute_lane_report(rows, floor=8)
    warm_vals = {s["value"] for s in rep["warm"]}
    cold_vals = {s["value"] for s in rep["cold"]}
    assert "greatboard" in warm_vals
    assert "coldboard" in cold_vals
    assert rep["n"] == 52


def test_annotate_job_signal_reads_flags_for_its_lanes():
    rows = [_app(True, True, "greatboard", "quant") for _ in range(12)]
    rows += [_app(False, False, "coldboard", "data") for _ in range(40)]
    rep = compute_lane_report(rows, floor=8)
    job = {"segments": {"source_board": "greatboard", "role_family": "quant",
                        "seniority": "mid", "score_band": "7", "fit_gap_category": "unknown",
                        "location_bucket": "remote", "salary_band": "unknown"}}
    sig = annotate_job_signal(job, rep)
    assert sig["flags"]["source_board"] == "warm"
    assert sig["top"] == "warm"


def test_thin_data_is_insufficient_not_warm():
    rows = [_app(True, True, "tiny", "quant")]
    rows += [_app(False, False, "bulk", "data") for _ in range(20)]
    rep = compute_lane_report(rows, floor=8)
    job = {"segments": {"source_board": "tiny", "role_family": "quant",
                        "seniority": "mid", "score_band": "7", "fit_gap_category": "unknown",
                        "location_bucket": "remote", "salary_band": "unknown"}}
    sig = annotate_job_signal(job, rep)
    assert sig["flags"]["source_board"] == "insufficient"
    assert sig["top"] in ("insufficient", "none")
