from __future__ import annotations

import sqlite3

from applypilot.apply.answer_exceptions import (
    approve_exception,
    list_exceptions,
    reconcile_resolved_exceptions,
    record_exceptions,
    resolve_approved_answer,
)


def connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_unresolved_question_is_deduplicated_and_approved_exactly():
    conn = connection()
    fields = [{
        "key": "question-1",
        "label": "Are you a customer or dealer associated with Acme?*",
        "options": ["Yes", "No"],
    }]
    first = record_exceptions(conn, fields, host="acme.wd1.myworkdayjobs.com", job_url="https://job/1")
    second = record_exceptions(conn, fields, host="acme.wd1.myworkdayjobs.com", job_url="https://job/2")
    assert first == second
    assert len(list_exceptions(conn, status="pending")) == 1

    approve_exception(conn, first[0], "No")

    assert resolve_approved_answer(
        conn, "Are you a customer or dealer associated with Acme?*",
        host="acme.wd1.myworkdayjobs.com",
    ) == "No"
    assert list_exceptions(conn, status="pending") == []


def test_host_specific_approval_does_not_leak_to_another_tenant():
    conn = connection()
    ids = record_exceptions(conn, [{"key": "q", "label": "Custom question?", "options": []}],
                            host="one.example", job_url="https://job/1")
    approve_exception(conn, ids[0], "Approved answer")
    assert resolve_approved_answer(conn, "Custom question?", host="two.example") is None


def test_field_key_specific_approval_does_not_leak_between_controls():
    conn = connection()
    ids = record_exceptions(
        conn,
        [{"key": "control-a", "label": "Choose a response?", "options": []}],
        host="one.example",
        job_url="https://job/1",
    )
    approve_exception(conn, ids[0], "Approved answer")

    assert resolve_approved_answer(
        conn, "Choose a response?", host="one.example", field_key="control-a"
    ) == "Approved answer"
    assert resolve_approved_answer(
        conn, "Choose a response?", host="one.example", field_key="control-b"
    ) is None


def test_answer_must_match_known_options_when_options_exist():
    conn = connection()
    ids = record_exceptions(conn, [{"key": "q", "label": "Choose?", "options": ["Yes", "No"]}],
                            host="one.example", job_url="https://job/1")
    try:
        approve_exception(conn, ids[0], "Maybe")
    except ValueError as exc:
        assert str(exc) == "answer_not_in_options"
    else:
        raise AssertionError("invalid option was accepted")


def test_reconcile_marks_only_resolved_pending_questions():
    conn = connection()
    ids = record_exceptions(conn, [
        {"key": "address", "label": "Address Line 1*", "options": []},
        {"key": "factual", "label": "Are you Hispanic/Latino?*", "options": ["Yes", "No"]},
    ], host="one.example", job_url="https://job/1")
    changed = reconcile_resolved_exceptions(
        conn,
        host="one.example",
        resolved_questions=["Address Line 1*"],
        resolution_source="profile",
    )
    assert changed == 1
    rows = list_exceptions(conn, status=None)
    assert rows[0]["id"] == ids[0]
    assert rows[0]["status"] == "resolved"
    assert rows[0]["resolution_source"] == "profile"
    assert rows[1]["id"] == ids[1]
    assert rows[1]["status"] == "pending"


def test_recurring_unresolved_question_reopens_after_deterministic_resolution():
    conn = connection()
    fields = [{"key": "source", "label": "How did you hear about us?", "options": []}]
    ids = record_exceptions(conn, fields, host="one.example", job_url="https://job/1")
    reconcile_resolved_exceptions(
        conn,
        host="one.example",
        resolved_questions=["How did you hear about us?"],
        resolution_source="current_field_plan",
    )

    record_exceptions(conn, fields, host="one.example", job_url="https://job/2")

    row = next(item for item in list_exceptions(conn, status=None) if item["id"] == ids[0])
    assert row["status"] == "pending"
