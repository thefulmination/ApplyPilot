from __future__ import annotations

from applypilot.inbox_auth import extract_verification_candidates, is_google_security_prompt


def test_extracts_high_confidence_greenhouse_code() -> None:
    candidates = extract_verification_candidates(
        subject="Your Greenhouse verification code",
        body="Use verification code 839214 to continue your application.",
        sender="no-reply@greenhouse.io",
    )

    assert candidates[0].kind == "code"
    assert candidates[0].value == "839214"
    assert candidates[0].confidence == "high"


def test_extracts_greenhouse_magic_link() -> None:
    candidates = extract_verification_candidates(
        subject="",
        body="Click https://boards.greenhouse.io/verify?token=abc123 to continue.",
        sender="no-reply@greenhouse.io",
    )

    assert candidates[0].kind == "magic_link"
    assert candidates[0].value.startswith("https://boards.greenhouse.io/verify?token=abc123")


def test_ignores_generic_numbers_without_verification_context() -> None:
    candidates = extract_verification_candidates(
        subject="",
        body="Founded in 2019. Call 415-747-2735 if needed. Job ID 123456789.",
        sender="recruiting@example.com",
    )

    assert candidates == []


def test_identifies_google_security_prompt() -> None:
    assert is_google_security_prompt(
        subject="Security alert",
        body="A new sign-in on Windows requires your passkey.",
        sender="no-reply@accounts.google.com",
    )
