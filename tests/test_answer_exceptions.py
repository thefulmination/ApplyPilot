from __future__ import annotations

import sqlite3

from applypilot.apply.answer_exceptions import (
    approve_exception,
    list_exceptions,
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
