"""Regression tests for the code-review hardening pass."""
from __future__ import annotations

from pathlib import Path


# -- SSRF guard (enrichment) -------------------------------------------------

def test_is_safe_public_url_blocks_internal_targets() -> None:
    from applypilot.enrichment.detail import is_safe_public_url

    assert is_safe_public_url("http://169.254.169.254/latest/meta-data") is False
    assert is_safe_public_url("http://127.0.0.1:8080/x") is False
    assert is_safe_public_url("http://10.0.0.5/jobs") is False
    assert is_safe_public_url("http://192.168.1.10/jobs") is False
    assert is_safe_public_url("ftp://example.com/x") is False  # non-http scheme
    assert is_safe_public_url("not a url") is False
    # A public hostname is allowed (or allowed-through if DNS can't resolve here).
    assert is_safe_public_url("https://boards.greenhouse.io/acme/jobs/1") is True


# -- Cover-letter fabrication enforcement ------------------------------------

def test_validate_cover_letter_flags_fabricated_tool() -> None:
    from applypilot.scoring.validator import validate_cover_letter

    profile = {"skills_boundary": {"languages": ["Python", "SQL"]}}
    text = "Dear Hiring Manager,\nI shipped services in Golang and Rust.\nThanks\nJane"

    result = validate_cover_letter(text, mode="lenient", profile=profile)

    assert result["passed"] is False
    assert any("Fabricated tool" in e for e in result["errors"])


def test_validate_cover_letter_passes_clean_letter() -> None:
    from applypilot.scoring.validator import validate_cover_letter

    profile = {"skills_boundary": {"languages": ["Python", "SQL"]}}
    text = "Dear Hiring Manager,\nI built data pipelines in Python and SQL.\nThanks\nJane"

    assert validate_cover_letter(text, mode="lenient", profile=profile)["passed"] is True


def test_validate_cover_letter_no_profile_skips_fabrication_scan() -> None:
    from applypilot.scoring.validator import validate_cover_letter

    # Back-compat: without a profile, the fabrication scan must not run.
    text = "Dear Hiring Manager,\nGolang expert here.\nThanks\nJane"
    assert validate_cover_letter(text, mode="lenient")["passed"] is True


def test_validate_cover_letter_word_boundary_no_false_positive() -> None:
    from applypilot.scoring.validator import validate_cover_letter

    profile = {"skills_boundary": {"languages": ["Python"]}}
    # "scalable" / "trust" / "offspring" contain watchlist substrings but must
    # not trip the non-alphanumeric-boundary fabrication check.
    text = ("Dear Hiring Manager,\nI build scalable systems you can trust.\n"
            "Thanks\nJane")
    assert validate_cover_letter(text, mode="lenient", profile=profile)["passed"] is True


# -- Robust JSON extraction (tailor) -----------------------------------------

def test_extract_json_handles_trailing_and_leading_prose() -> None:
    from applypilot.scoring.tailor import extract_json

    assert extract_json('{"a": 1} and some trailing prose with a } brace') == {"a": 1}
    assert extract_json('Here is the JSON: {"x": {"y": 2}} done.') == {"x": {"y": 2}}
    assert extract_json('```json\n{"k": [1, 2, 3]}\n```') == {"k": [1, 2, 3]}


# -- Wizard salary parsing ---------------------------------------------------

def test_parse_salary_token() -> None:
    from applypilot.wizard.init import _parse_salary_token

    assert _parse_salary_token("$80,000", "0") == "80000"
    assert _parse_salary_token("120k", "0") == "120000"
    assert _parse_salary_token("  95000 ", "0") == "95000"
    assert _parse_salary_token("not a number", "99") == "99"
    assert _parse_salary_token("", "fallback") == "fallback"


# -- record_application writes company, not site -----------------------------

def test_record_application_uses_company_column(tmp_path: Path, monkeypatch) -> None:
    from applypilot import database, applications

    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(applications, "get_connection", lambda: conn)
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, source_board, application_url) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("https://x.com/j1", "Engineer", "LinkedIn", "RealCorp", "linkedin", "https://x.com/apply"),
    )
    conn.commit()

    applications.record_application("https://x.com/j1", status="applied", update_job=False)

    row = conn.execute(
        "SELECT company, source FROM applications WHERE job_url = ?",
        ("https://x.com/j1",),
    ).fetchone()
    assert row["company"] == "RealCorp"   # company column gets the company, not the board
    assert row["source"] == "linkedin"    # source column gets the board
