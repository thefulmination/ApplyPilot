import sqlite3
import subprocess
import sys

from applypilot.scoring import scorer


def test_select_description_returns_short_text_unchanged():
    text = "Short role description with requirements up front."

    assert scorer.select_description(text, cap=15000) == text


def test_select_description_empty_values_return_empty_string():
    assert scorer.select_description(None) == ""
    assert scorer.select_description("") == ""


def test_select_description_preserves_late_requirements_section_with_markers():
    head = "Intro " + ("A" * 15990)
    tail = "Requirements\n" + ("B" * 900) + "SENTINEL-LATE-REQUIREMENT"
    selected = scorer.select_description(head + tail, cap=15000)

    assert len(selected) <= 15000 + len(scorer.DESCRIPTION_TRUNCATION_MARKER)
    assert selected.startswith("Intro ")
    assert "SENTINEL-LATE-REQUIREMENT" in selected
    assert scorer.DESCRIPTION_OMISSION_MARKER in selected
    assert scorer.DESCRIPTION_TRUNCATION_MARKER in selected


def test_select_description_without_marker_truncates_plainly():
    selected = scorer.select_description("X" * 200, cap=25)

    assert selected == ("X" * 25) + scorer.DESCRIPTION_TRUNCATION_MARKER
    assert scorer.DESCRIPTION_OMISSION_MARKER not in selected


def test_build_score_prompt_text_includes_late_requirements_sentinel():
    desc = ("A" * 7000) + "\nQualifications\nSENTINEL-BEYOND-OLD-CUT"
    job = {"title": "T", "site": "S", "full_description": desc}

    prompt = scorer.build_score_prompt_text("RESUME", job)

    assert "SENTINEL-BEYOND-OLD-CUT" in prompt


def test_score_job_sends_late_requirements_sentinel(monkeypatch):
    seen = {}

    class FakeClient:
        model = "fake-model"
        provider_name = "fake-provider"

        def chat(self, *a, **k):
            seen["messages"] = a[0]
            return "SCORE: 8\nKEYWORDS: requirements\nVERDICT: Good fit - test\nREASONING: test"

    monkeypatch.setattr(scorer, "get_client", lambda **_kwargs: FakeClient())
    desc = ("A" * 7000) + "\nRequirements\nSENTINEL-IN-LLM-MESSAGE"
    job = {"title": "T", "site": "S", "full_description": desc}

    out = scorer.score_job("RESUME", job)

    assert out["score"] == 8
    assert "SENTINEL-IN-LLM-MESSAGE" in seen["messages"][1]["content"]


def test_rescore_desc_cut_script_lists_late_marker_rows(tmp_path):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE jobs (url TEXT, title TEXT, site TEXT, fit_score INTEGER, full_description TEXT)"
    )
    conn.execute(
        "INSERT INTO jobs VALUES (?,?,?,?,?)",
        ("u1", "Role", "Co", 8, ("A" * 6500) + "\nRequirements\nSENTINEL"),
    )
    conn.execute(
        "INSERT INTO jobs VALUES (?,?,?,?,?)",
        ("u2", "Role2", "Co2", 7, "Requirements\n" + ("B" * 6500)),
    )
    conn.commit()
    conn.close()

    result = subprocess.run(
        [sys.executable, "rescore-desc-cut.py", "--db", str(db), "--format", "csv"],
        cwd=".",
        text=True,
        capture_output=True,
        check=True,
    )

    assert "url,title,site,fit_score,desc_len" in result.stdout
    assert "u1,Role,Co,8," in result.stdout
    assert "u2" not in result.stdout
    assert "1 rows" in result.stderr
