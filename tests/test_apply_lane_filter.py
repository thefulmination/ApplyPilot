"""Off-lane drift guard (APPLYPILOT_LANE_FILTER).

A real run, after the on-lane (Chief of Staff / strategy-ops) queue drained, drifted
down to pure IC-sales postings that still scored >=7 -- "Sales Engineer-Flooring",
"Enterprise AE". The apply ORDER BY ranks on-lane roles first but never EXCLUDES the
off-lane ones, so the run applied to them anyway. With the lane filter on, acquire_job
drops off-lane titles (unless an on-lane audit flag rescues them) and any posting the
diagnoser explicitly labelled wrong-lane / ignore. Default OFF -> normal runs unchanged.
"""
from __future__ import annotations

import pytest

from applypilot import database
from applypilot.apply import launcher as L


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(L, "get_connection", lambda: c)
    return c


def _ins(conn, url, title, *, audit=8.0, flags=None, gap=None, action=None, company="Acme"):
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, tailored_resume_path, "
        "fit_score, audit_score, audit_flags, fit_gap_category, recommended_action) VALUES "
        "(?, ?, 'X', ?, ?, 'x', 8, ?, ?, ?, ?)",
        (url, title, company, "https://boards.greenhouse.io/acme/" + url[-3:], audit, flags, gap, action),
    )
    conn.commit()


def test_filter_off_by_default_applies_to_off_lane(conn):
    # No env -> filter off -> an off-lane AE role is still acquirable (unchanged behavior).
    _ins(conn, "https://x/ae1", "Enterprise Account Executive")
    assert L.acquire_job(min_score=7) is not None


def test_off_lane_title_excluded_when_on(conn, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_LANE_FILTER", "1")
    _ins(conn, "https://x/ae2", "Enterprise Account Executive")
    _ins(conn, "https://x/se3", "Sales Engineer - Flooring")
    _ins(conn, "https://x/ae4", "Enterprise AE")  # abbreviation, word-boundary needle
    assert L.acquire_job(min_score=7) is None  # all off-lane -> queue effectively empty


def test_on_lane_role_still_acquired_with_filter_on(conn, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_LANE_FILTER", "1")
    _ins(conn, "https://x/ae5", "Enterprise Account Executive")          # off-lane
    _ins(conn, "https://x/cos6", "Chief of Staff", audit=8.5)            # on-lane, higher
    job = L.acquire_job(min_score=7)
    assert job is not None and job["url"].endswith("/cos6")


def test_on_lane_audit_flag_overrides_off_lane_title(conn, monkeypatch):
    # A title with an off-lane needle but a genuine on-lane flag is NOT excluded
    # (the flag is a positive override -- e.g. "Chief of Staff, Sales").
    monkeypatch.setenv("APPLYPILOT_LANE_FILTER", "1")
    _ins(conn, "https://x/cs7", "Chief of Staff, Sales", flags='["chief_of_staff"]')
    job = L.acquire_job(min_score=7)
    assert job is not None and job["url"].endswith("/cs7")


def test_diagnosed_wrong_lane_excluded(conn, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_LANE_FILTER", "1")
    # An on-lane-looking title the diagnoser explicitly flagged wrong-lane is dropped.
    _ins(conn, "https://x/wl8", "Operations Manager", gap="wrong_role_lane")
    _ins(conn, "https://x/ig9", "Operations Manager", action="ignore", company="Globex")
    assert L.acquire_job(min_score=7) is None


def test_word_boundary_needle_no_false_positive(conn, monkeypatch):
    # " ae " must not fire inside another word (e.g. a fictional "Caesar" team won't
    # match; "Praetorian" won't match). Use a benign on-lane title containing "ae"-ish
    # substrings to confirm the padding works.
    monkeypatch.setenv("APPLYPILOT_LANE_FILTER", "1")
    _ins(conn, "https://x/ok10", "Strategy and Operations Lead")  # no off-lane needle
    job = L.acquire_job(min_score=7)
    assert job is not None and job["url"].endswith("/ok10")
