"""PG-backed tests for the screening-question answer bank (R10 / spec 9.2).

The load-bearing property is FAIL-SAFE: an unknown question NEVER yields a
guessed answer -- it returns None (worker defers) AND is recorded on the owner's
defer queue. A second worker hitting the same unknown must not blow up or
duplicate it. Once the owner answers, get returns the vetted answer.

Run from the repo root:
    ".conda-env/python.exe" -m pytest tests/test_fleet_v3_answer_bank.py -q
"""
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import answer_bank


# ---------------------------------------------------------------------------
# normalize_question
# ---------------------------------------------------------------------------

def test_normalize_strips_punct_and_collapses_ws():
    assert (
        answer_bank.normalize_question("Are you authorized to work in the US?")
        == "are you authorized to work in the us"
    )


def test_normalize_equates_phrasing_variants():
    a = answer_bank.normalize_question("Are you authorized to work in the US?")
    b = answer_bank.normalize_question("  ARE you authorized   to work in the US  ")
    assert a == b == "are you authorized to work in the us"


def test_normalize_handles_none_and_blank():
    assert answer_bank.normalize_question(None) == ""
    assert answer_bank.normalize_question("   ") == ""


# ---------------------------------------------------------------------------
# get_answer: fail-safe on unknown
# ---------------------------------------------------------------------------

def test_unknown_returns_none_and_records_deferred(fleet_db):
    q = "What is your desired salary?"
    with pgqueue.connect(fleet_db) as conn:
        # Never seen -> must defer (None), not guess.
        assert answer_bank.get_answer(conn, q) is None

        with conn.cursor() as cur:
            cur.execute(
                "SELECT q_norm, q_raw, answer, status FROM answer_bank WHERE q_norm = %s",
                (answer_bank.normalize_question(q),),
            )
            row = cur.fetchone()
        assert row is not None
        assert row["status"] == "unknown_deferred"
        assert row["q_raw"] == q            # original phrasing preserved for the owner
        assert row["answer"] is None        # NEVER a fabricated answer


def test_repeated_unknown_is_idempotent(fleet_db):
    q = "How many years of Python experience do you have?"
    with pgqueue.connect(fleet_db) as conn:
        assert answer_bank.get_answer(conn, q) is None
        # A second worker hits the same unknown: still None, no duplicate row, no error.
        assert answer_bank.get_answer(conn, q) is None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM answer_bank WHERE q_norm = %s",
                (answer_bank.normalize_question(q),),
            )
            assert cur.fetchone()["n"] == 1
    assert answer_bank.normalize_question(q)  # sanity: non-empty key


# ---------------------------------------------------------------------------
# set_answer + get_answer: known round-trip
# ---------------------------------------------------------------------------

def test_set_then_get_returns_known(fleet_db):
    q = "Are you authorized to work in the US?"
    with pgqueue.connect(fleet_db) as conn:
        answer_bank.set_answer(conn, q, "Yes", kind="work_auth")
        # Same question, different casing/whitespace -> normalized to the same key.
        assert answer_bank.get_answer(conn, "  are you authorized to work in the US ") == "Yes"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, kind FROM answer_bank WHERE q_norm = %s",
                (answer_bank.normalize_question(q),),
            )
            row = cur.fetchone()
        assert row["status"] == "known"
        assert row["kind"] == "work_auth"


def test_set_answer_promotes_a_deferred_unknown(fleet_db):
    q = "Will you now or in the future require sponsorship?"
    with pgqueue.connect(fleet_db) as conn:
        assert answer_bank.get_answer(conn, q) is None          # recorded deferred
        assert answer_bank.list_unknown(conn)                   # on the defer queue
        answer_bank.set_answer(conn, q, "No")                   # owner answers it
        assert answer_bank.get_answer(conn, q) == "No"
        assert answer_bank.list_unknown(conn) == []             # cleared from defer queue


# ---------------------------------------------------------------------------
# list_unknown: the owner defer queue
# ---------------------------------------------------------------------------

def test_list_unknown_shows_only_deferred(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        answer_bank.set_answer(conn, "Are you authorized to work in the US?", "Yes")
        answer_bank.get_answer(conn, "Describe a time you led a team.")   # unknown -> deferred
        answer_bank.get_answer(conn, "What is your notice period?")       # unknown -> deferred

        unknown = answer_bank.list_unknown(conn)
        norms = {u["q_norm"] for u in unknown}
    assert norms == {
        answer_bank.normalize_question("Describe a time you led a team."),
        answer_bank.normalize_question("What is your notice period?"),
    }
    assert all(u["status"] == "unknown_deferred" for u in unknown)


# ---------------------------------------------------------------------------
# seed_known: bulk load
# ---------------------------------------------------------------------------

def test_seed_known_bulk_loads(fleet_db):
    pairs = {
        "Are you authorized to work in the US?": "Yes",
        "Do you require visa sponsorship?": "No",
        "Are you willing to relocate?": "Yes",
    }
    with pgqueue.connect(fleet_db) as conn:
        assert answer_bank.seed_known(conn, pairs) == 3
        for q, a in pairs.items():
            assert answer_bank.get_answer(conn, q) == a
        assert answer_bank.list_unknown(conn) == []
