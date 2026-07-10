from applypilot.apply.greenhouse_shadow_main import inventory_url


PROFILE = {
    "personal": {"full_name": "Jane Doe", "email": "jane@example.com"},
    "work_authorization": {},
}


def test_inventory_is_read_only_and_does_not_call_model():
    payload = {
        "questions": [
            {
                "label": "Name",
                "required": True,
                "fields": [
                    {"name": "first_name", "type": "input_text"},
                    {"name": "last_name", "type": "input_text"},
                    {"name": "email", "type": "input_text"},
                ],
            },
            {
                "label": "Why us?",
                "required": True,
                "fields": [{"name": "question_1", "type": "textarea"}],
            },
        ]
    }

    result = inventory_url(
        "https://job-boards.greenhouse.io/acme/jobs/123",
        profile=PROFILE,
        fetch=lambda url: payload,
    )

    assert result.question_count == 2
    assert result.required_count == 2
    assert result.unmapped_required == ["Why us?"]
    assert result.ready_without_free_text is False
    assert result.error is None


def test_inventory_rejects_non_greenhouse_url_without_fetching():
    result = inventory_url(
        "https://example.com/jobs/123",
        profile=PROFILE,
        fetch=lambda url: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )

    assert result.error == "unsupported_url"
    assert result.question_count == 0
