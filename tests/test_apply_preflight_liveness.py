"""Pre-launch liveness probe (APPLYPILOT_PREFLIGHT_LIVENESS) + apply-time closure feedback.

A closed-but-HTTP-200 posting (role filled/closed but the page still renders) used to
burn a full Chrome launch + agent run (~$1.50) before the agent reached RESULT:EXPIRED.
With the preflight on, the worker HTTP-probes each candidate first and skips a strong-
DEAD posting before launching anything. The probe MUST be conservative: an anonymous
GET to LinkedIn (999) / Cloudflare (403) / a rate-limited host (429) is UNCERTAIN, never
DEAD -- otherwise the preflight would false-skip live jobs. Apply-time closures
(RESULT:EXPIRED) are also fed back as a liveness fact.
"""
from __future__ import annotations

import pytest

from applypilot import database
from applypilot.apply import launcher as L
from applypilot.apply import liveness


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(L, "get_connection", lambda: c)
    return c


def test_preflight_enabled_reads_env(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_PREFLIGHT_LIVENESS", raising=False)
    assert L._preflight_liveness_enabled() is True
    monkeypatch.setenv("APPLYPILOT_PREFLIGHT_LIVENESS", "0")
    assert L._preflight_liveness_enabled() is False
    monkeypatch.setenv("APPLYPILOT_PREFLIGHT_LIVENESS", "on")
    assert L._preflight_liveness_enabled() is True


def test_stamp_dead_excludes_from_acquire_and_is_retained(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, tailored_resume_path, "
        "fit_score, audit_score) VALUES ('https://x/dead', 'Chief of Staff', 'X', 'Acme', "
        "'https://boards.greenhouse.io/acme/1', 'x', 8, 9.0)")
    conn.commit()
    assert L.acquire_job(min_score=7) is not None  # acquirable before
    L.release_lock("https://x/dead")               # drop the lease acquire just took
    L._stamp_liveness_dead("https://x/dead", "preflight_text:'position has been filled'")
    row = conn.execute(
        "SELECT liveness_status, liveness_reason FROM jobs WHERE url='https://x/dead'").fetchone()
    assert row[0] == "dead" and "filled" in row[1]
    # excluded by the liveness != 'dead' acquire filter, but the row still exists (retained)
    assert L.acquire_job(min_score=7) is None
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE url='https://x/dead'").fetchone()[0] == 1


def test_dead_on_visit_reasons_membership():
    assert "expired" in L.DEAD_ON_VISIT_REASONS
    assert "page_error" not in L.DEAD_ON_VISIT_REASONS  # transient (500/blank), not a closure


# --- probe conservatism: the property that makes the preflight safe -----------

def _patch_fetch(monkeypatch, status, body="", final=None):
    monkeypatch.setattr(liveness, "_fetch",
                        lambda url, accept=None: (status, final or url, body))


def test_probe_dead_on_http_404(monkeypatch):
    _patch_fetch(monkeypatch, 404)
    assert liveness.probe_url("https://careers.example.com/job/1")[0] == liveness.DEAD


def test_probe_dead_on_closure_text(monkeypatch):
    _patch_fetch(monkeypatch, 200, body="<html>This position has been filled. Thank you!</html>")
    assert liveness.probe_url("https://careers.example.com/job/2")[0] == liveness.DEAD


def test_probe_uncertain_on_linkedin_999(monkeypatch):
    # LinkedIn answers anonymous GETs with 999 -> must be UNCERTAIN, never DEAD, or the
    # preflight would false-skip every LinkedIn job.
    _patch_fetch(monkeypatch, 999)
    assert liveness.probe_url("https://www.linkedin.com/jobs/view/3")[0] != liveness.DEAD


def test_probe_uncertain_on_block_and_server_codes(monkeypatch):
    for code in (401, 403, 429, 500, 503):
        _patch_fetch(monkeypatch, code)
        st = liveness.probe_url("https://careers.example.com/job/4")[0]
        assert st == liveness.UNCERTAIN, f"{code} -> {st}, expected uncertain"


def test_probe_live_on_plain_200(monkeypatch):
    _patch_fetch(monkeypatch, 200, body="<html>Apply now for this great role</html>" + "x" * 2000)
    assert liveness.probe_url("https://careers.example.com/job/5")[0] == liveness.LIVE
