from __future__ import annotations

from applypilot.cli import _llm_status_from_env, _site_rows_for_status


def test_doctor_reports_deepseek_when_deepseek_is_configured() -> None:
    env = {
        "GEMINI_API_KEY": "gemini-key",
        "DEEPSEEK_API_KEY": "deepseek-key",
        "LLM_PROVIDER": "deepseek",
        "LLM_MODEL": "deepseek-v4-pro",
    }

    status, note = _llm_status_from_env(env)

    assert status == "ok"
    assert note == "DeepSeek (deepseek-v4-pro)"


def test_status_source_rows_are_truncated_by_default() -> None:
    rows = [(f"Source {idx}", idx) for idx in range(100, 0, -1)]

    visible, hidden = _site_rows_for_status(rows, top_sites=50, all_sites=False)

    assert len(visible) == 50
    assert hidden == 50
    assert visible[0] == ("Source 100", 100)


def test_status_source_rows_can_show_all() -> None:
    rows = [(f"Source {idx}", idx) for idx in range(3)]

    visible, hidden = _site_rows_for_status(rows, top_sites=1, all_sites=True)

    assert visible == rows
    assert hidden == 0
