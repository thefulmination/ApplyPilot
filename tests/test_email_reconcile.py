from applypilot.fleet import email_reconcile as er
import sqlite3


def test_classify_strong_method_is_confirmed_regardless_of_score():
    assert er.classify_match("company_domain", 1.0) == "confirmed"
    assert er.classify_match("board_slug", 1.0) == "confirmed"
    assert er.classify_match("linkedin_job_id", 1.0) == "confirmed"


def test_classify_fuzzy_at_or_above_threshold_is_confirmed():
    assert er.classify_match("company_name", 0.6) == "confirmed"
    assert er.classify_match("title", 0.75) == "confirmed"


def test_classify_fuzzy_below_threshold_is_probable():
    assert er.classify_match("company_name", 0.59) == "probable"
    assert er.classify_match("ats_domain", 0.25) == "probable"


def test_classify_no_match_is_none():
    assert er.classify_match(None, None) is None
