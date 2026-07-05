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


def _make_profile() -> dict:
    return {
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


def _patch_prompt_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(prompt.config, "load_profile", _make_profile)
    monkeypatch.setattr(prompt.config, "load_search_config",
                        lambda: {"location": {"accept_patterns": ["New York"]}})
    monkeypatch.setattr(prompt.config, "load_blocked_sso", lambda: ["accounts.google.com"])
    monkeypatch.setattr(prompt.config, "APPLY_WORKER_DIR", tmp_path / "apply_worker")


def test_build_prompt_stages_uploads_per_worker(tmp_path: Path, monkeypatch) -> None:
    # Parallel workers must not share one upload path, or they overwrite each
    # other's tailored resume between the copy and the agent's upload.
    tailored_txt = tmp_path / "tailored.txt"
    tailored_txt.write_text("Tailored resume text", encoding="utf-8")
    tailored_txt.with_suffix(".pdf").write_bytes(b"%PDF-1.4\n")

    _patch_prompt_config(monkeypatch, tmp_path)
    job = {
        "url": "https://example.com/job",
        "title": "Chief of Staff",
        "site": "ExampleCo",
        "fit_score": 9,
        "tailored_resume_path": str(tailored_txt),
    }

    text0 = prompt.build_prompt(job, "Tailored resume text", worker_id=0)
    text3 = prompt.build_prompt(job, "Tailored resume text", worker_id=3)

    assert "worker-0" in text0 and "worker-3" not in text0
    assert "worker-3" in text3 and "worker-0" not in text3


def test_screening_section_is_truthful_not_swe_templated() -> None:
    # The screening guidance used to be hard-templated for a software engineer and
    # told the agent to answer YES to any DevOps/ML/cloud tool because "software
    # engineers learn tools fast" -- wrong domain for this candidate AND a bias to
    # over-claim. It must now be domain-neutral and truthful.
    section = prompt._build_screening_section(_make_profile())
    low = section.lower()
    assert "software engineers learn tools fast" not in low
    assert "devops, backend, ml, cloud" not in low
    # Targets the candidate's real role, confidently but truthfully.
    assert "Chief of Staff" in section
    assert "truthful" in low and "could pick it up" in low  # don't claim merely-learnable skills
    # Behavioral answers must never be fabricated.
    assert "tell me about a time" in low
    assert "never invent" in low and "resume" in low


def test_screening_section_allows_us_relocation_when_configured() -> None:
    section = prompt._build_screening_section(
        _make_profile(),
        {
            "location": {
                "accept_any_us": True,
                "accept_patterns": ["San Francisco", "New York", "Remote", "United States"],
            }
        },
    )
    low = section.lower()
    assert "cannot relocate" not in low
    assert "san francisco" in low
    assert "new york" in low
    assert "willing to relocate anywhere in the united states" in low


def test_screening_target_role_no_swe_default() -> None:
    # With no target_role/current_job_title, the fallback must NOT be "software engineer".
    profile = _make_profile()
    profile["experience"].pop("target_role", None)
    profile["personal"].pop("current_job_title", None)
    section = prompt._build_screening_section(profile)
    assert "software engineer" not in section.lower()
    assert "this role" in section


def test_build_prompt_dry_run_emits_dry_run_code(tmp_path: Path, monkeypatch) -> None:
    tailored_txt = tmp_path / "tailored.txt"
    tailored_txt.write_text("Tailored resume text", encoding="utf-8")
    tailored_txt.with_suffix(".pdf").write_bytes(b"%PDF-1.4\n")

    _patch_prompt_config(monkeypatch, tmp_path)
    job = {
        "url": "https://example.com/job",
        "title": "Chief of Staff",
        "site": "ExampleCo",
        "fit_score": 9,
        "tailored_resume_path": str(tailored_txt),
    }

    text = prompt.build_prompt(job, "Tailored resume text", dry_run=True)

    assert "RESULT:DRY_RUN" in text
    assert "Do NOT output RESULT:APPLIED" in text


def test_captcha_prompt_exits_fast_on_unsupported_capsolver_errors(monkeypatch) -> None:
    monkeypatch.setenv("CAPSOLVER_API_KEY", "CAI-test-key")
    monkeypatch.setattr(prompt.config, "load_env", lambda: None)

    text = prompt._build_captcha_section()

    assert "ERROR_INVALID_TASK_DATA" in text
    assert "ERROR_TASK_NOT_SUPPORTED" in text
    assert "errorCode" in text
    assert "RESULT:CAPTCHA" in text
    assert "do not keep trying accessibility" in text.lower()
