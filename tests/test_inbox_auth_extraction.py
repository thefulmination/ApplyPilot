from __future__ import annotations

from applypilot.inbox_auth import extract_verification_candidates, is_google_security_prompt, now_utc


def test_extracts_high_confidence_greenhouse_code() -> None:
    candidates = extract_verification_candidates(
        subject="Your Greenhouse verification code",
        body="Use verification code 839214 to continue your application.",
        sender="no-reply@greenhouse.io",
    )

    assert candidates
    assert candidates[0].kind == "code"
    assert candidates[0].value == "839214"
    assert candidates[0].confidence == "high"


def test_extracts_greenhouse_magic_link() -> None:
    candidates = extract_verification_candidates(
        subject="",
        body="Click https://boards.greenhouse.io/verify?token=abc123 to continue.",
        sender="no-reply@greenhouse.io",
    )

    assert candidates
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


def test_ignores_embedded_digits_in_letters() -> None:
    candidates = extract_verification_candidates(
        subject="Your verification code",
        body="Use verification code AB1234CD to continue your application.",
        sender="no-reply@greenhouse.io",
    )

    assert candidates == []


def test_ignores_greenhouse_zip_and_postal_codes() -> None:
    candidates = extract_verification_candidates(
        subject="Confirm your Greenhouse application address",
        body="Please confirm your address. ZIP code 94105 and postal code 94107 must match your profile.",
        sender="no-reply@greenhouse.io",
    )

    assert candidates == []


def test_ignores_tracking_click_url_with_token() -> None:
    candidates = extract_verification_candidates(
        subject="Confirm your email",
        body="Click https://click.example-mail.com/track?token=abc123 to confirm your email.",
        sender="recruiting@example.com",
    )

    assert candidates == []


def test_generic_security_prompt_suppresses_candidates() -> None:
    assert is_google_security_prompt(
        subject="Security alert",
        body="A new sign-in on Windows requires your passkey. Use 123456 to continue.",
        sender="alerts@example.com",
    )
    assert (
        extract_verification_candidates(
            subject="Security alert",
            body="A new sign-in on Windows requires your passkey. Use 123456 to continue.",
            sender="alerts@example.com",
        )
        == []
    )


def test_candidates_expose_message_position() -> None:
    candidates = extract_verification_candidates(
        subject="Your Greenhouse verification code",
        body="Use verification code 839214 to continue your application.",
        sender="no-reply@greenhouse.io",
    )

    assert candidates
    assert isinstance(candidates[0].position, int)
    assert candidates[0].position >= 0


def test_same_confidence_candidates_sort_by_message_position() -> None:
    candidates = extract_verification_candidates(
        subject="Your verification codes",
        body="First enter verification code 111111. Then use verification code 222222.",
        sender="no-reply@greenhouse.io",
    )

    assert candidates
    assert [candidate.value for candidate in candidates] == ["111111", "222222"]


def test_now_utc_returns_iso_timestamp_text() -> None:
    timestamp = now_utc()

    assert isinstance(timestamp, str)
    assert timestamp.endswith("+00:00")


def test_equal_confidence_prefers_later_code_over_earlier_magic_link() -> None:
    candidates = extract_verification_candidates(
        subject="Your Greenhouse verification code",
        body=(
            "Click https://boards.greenhouse.io/verify?token=abc123 first. "
            "Then use verification code 123456."
        ),
        sender="no-reply@greenhouse.io",
    )

    assert candidates
    assert candidates[0].kind == "code"
    assert candidates[0].value == "123456"


def test_ignores_labeled_non_auth_codes_with_punctuation() -> None:
    bodies = [
        "ZIP code: 94105",
        "postal code is 94107",
        "job ID: 123456",
        "Reference code: 123456",
        "support # 123456",
    ]

    for body in bodies:
        assert (
            extract_verification_candidates(
                subject="Your verification code",
                body=body,
                sender="no-reply@greenhouse.io",
            )
            == []
        )


def test_does_not_extract_url_query_number_as_code() -> None:
    candidates = extract_verification_candidates(
        subject="Your verification code",
        body="Click https://boards.greenhouse.io/verify?token=123456 to continue.",
        sender="no-reply@greenhouse.io",
    )

    assert all(candidate.kind != "code" for candidate in candidates)


def test_ignores_known_ats_unsubscribe_and_pixel_urls() -> None:
    for url in (
        "https://boards.greenhouse.io/unsubscribe?token=abc123",
        "https://boards.greenhouse.io/pixel/open?token=abc123",
    ):
        assert (
            extract_verification_candidates(
                subject="Confirm your email",
                body=f"Click {url} to confirm your email.",
                sender="no-reply@greenhouse.io",
            )
            == []
        )


def test_accepts_workday_two_step_verification_code() -> None:
    candidates = extract_verification_candidates(
        subject="Your Workday verification code",
        body="Use verification code 123456 for two-step verification.",
        sender="no-reply@myworkday.com",
    )

    assert candidates
    assert candidates[0].kind == "code"
    assert candidates[0].value == "123456"
