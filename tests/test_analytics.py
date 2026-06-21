from __future__ import annotations


def test_llm_usage_recording_and_summary(tmp_path):
    from applypilot import database

    conn = database.init_db(tmp_path / "a.db")
    database.record_llm_usage(
        "score", "gemini-2.5-flash", "gemini",
        {"prompt_tokens": 1000, "completion_tokens": 200, "thinking_tokens": 0, "total_tokens": 1200},
        est_cost_usd=0.001, conn=conn,
    )
    database.record_llm_usage(
        "tailor", "gemini-2.5-flash", "gemini",
        {"prompt_tokens": 5000, "completion_tokens": 800, "total_tokens": 5800},
        conn=conn,
    )
    database.record_llm_usage("score", "gemini-2.5-flash", "gemini", None, conn=conn)  # no-op

    s = database.get_llm_usage_summary(conn)
    assert s["total_calls"] == 2  # the None usage was skipped
    assert s["total_tokens"] == 7000
    stages = {r["stage"]: r for r in s["by_stage"]}
    assert stages["score"]["prompt"] == 1000
    assert stages["tailor"]["total"] == 5800


def test_apply_analytics(tmp_path):
    from applypilot import database

    conn = database.init_db(tmp_path / "b.db")
    conn.executemany(
        "INSERT INTO jobs (url, title, site, apply_status, apply_error, apply_duration_ms) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("u1", "T", "LinkedIn", "applied", None, 30000),
            ("u2", "T", "LinkedIn", "failed", "captcha", None),
            ("u3", "T", "Greenhouse", "applied", None, 40000),
            ("u4", "T", "Greenhouse", "auth_required", "auth_required", None),
            ("u5", "T", "Indeed", None, None, None),          # unattempted
            ("u6", "T", "Indeed", "in_progress", None, None),  # in flight
        ],
    )
    conn.commit()

    a = database.get_apply_analytics(conn)
    assert a["applied"] == 2
    assert a["attempted"] == 4                  # 2 applied + 2 terminal failures
    assert abs(a["success_rate"] - 0.5) < 1e-9
    assert a["avg_apply_seconds"] == 35.0       # (30000 + 40000) / 2 / 1000
    sites = {r["site"]: r for r in a["by_site"]}
    assert sites["LinkedIn"]["applied"] == 1 and sites["LinkedIn"]["failed"] == 1
    reasons = {r["reason"] for r in a["fail_reasons"]}
    assert "captcha" in reasons and "auth_required" in reasons
