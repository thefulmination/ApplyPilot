from __future__ import annotations

from pathlib import Path

from applypilot.apply import prompt


def test_auto_apply_prompt_does_not_include_saved_password(tmp_path: Path, monkeypatch) -> None:
    tailored_txt = tmp_path / "tailored.txt"
    tailored_txt.write_text("Tailored resume text", encoding="utf-8")
    tailored_txt.with_suffix(".pdf").write_bytes(b"%PDF-1.4\n")

    profile = {
        "personal": {
            "full_name": "Jane Candidate",
            "preferred_name": "Jane",
            "email": "jane@example.com",
            "phone": "555-0100",
            "city": "New York",
            "province_state": "NY",
            "country": "US",
            "postal_code": "10001",
            "address": "",
            "password": "do-not-leak-this-password",
        },
        "work_authorization": {
            "legally_authorized_to_work": True,
            "require_sponsorship": False,
            "work_permit_type": "US Citizen",
        },
        "compensation": {
            "salary_expectation": "120000",
            "salary_currency": "USD",
            "salary_range_min": "120000",
            "salary_range_max": "160000",
        },
        "experience": {
            "years_of_experience_total": "6",
            "education_level": "Bachelor's",
            "target_role": "Chief of Staff",
        },
    }

    monkeypatch.setattr(prompt.config, "load_profile", lambda: profile)
    monkeypatch.setattr(prompt.config, "load_search_config", lambda: {"location": {"accept_patterns": ["New York"]}})
    monkeypatch.setattr(prompt.config, "load_blocked_sso", lambda: ["accounts.google.com"])
    monkeypatch.setattr(prompt.config, "APPLY_WORKER_DIR", tmp_path / "apply_worker")

    job = {
        "url": "https://example.com/job",
        "title": "Chief of Staff",
        "site": "ExampleCo",
        "fit_score": 9,
        "tailored_resume_path": str(tailored_txt),
    }

    text = prompt.build_prompt(job, "Tailored resume text")

    assert "do-not-leak-this-password" not in text
    assert "Use browser-stored credentials/session" in text
