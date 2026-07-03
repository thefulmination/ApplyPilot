"""Tests for supervised-mode tenant accounting.

Owner decision 2026-07-03 (spec amendment 0b2fead) dropped the pause-and-confirm
gate built in commit 6565936: supervised mode is now a full headed apply (agent
submits normally, owner watches + can Ctrl-C). This file used to test the
confirm truth-table (resolve_supervised_confirm), which no longer exists.

It now tests the accounting that replaced it: launcher.record_tenant_outcome
records the run's REAL terminal status against the tenant registry exactly
once, and run_job only invokes it when supervised=True. This fixes-by-construction
the review-caught bug where the old gate called record_submit(ok=True) on the
owner's "y" keystroke BEFORE any real submit happened.
"""
from __future__ import annotations

from applypilot.apply import launcher
from applypilot.apply.launcher import record_tenant_outcome


# ---------------------------------------------------------------------------
# record_tenant_outcome: pure(ish) accounting helper, unit-tested directly
# ---------------------------------------------------------------------------

def test_record_tenant_outcome_applied_is_ok_true(monkeypatch) -> None:
    """A terminal 'applied' status records ok=True, result='applied', exactly once."""
    calls = []

    def fake_record_submit(conn, host, *, ok, result):
        calls.append({"conn": conn, "host": host, "ok": ok, "result": result})

    monkeypatch.setattr(launcher.tenants_mod, "record_submit", fake_record_submit)

    conn = object()
    record_tenant_outcome(conn, "https://boards.greenhouse.io/acme/jobs/123", "applied")

    assert len(calls) == 1
    assert calls[0]["ok"] is True
    assert calls[0]["result"] == "applied"
    assert calls[0]["conn"] is conn


def test_record_tenant_outcome_failure_is_ok_false(monkeypatch) -> None:
    """A terminal failure status records ok=False with that status as the result,
    exactly once."""
    calls = []

    def fake_record_submit(conn, host, *, ok, result):
        calls.append({"conn": conn, "host": host, "ok": ok, "result": result})

    monkeypatch.setattr(launcher.tenants_mod, "record_submit", fake_record_submit)

    conn = object()
    record_tenant_outcome(conn, "https://boards.greenhouse.io/acme/jobs/123",
                           "failed:no_confirmation")

    assert len(calls) == 1
    assert calls[0]["ok"] is False
    assert calls[0]["result"] == "failed:no_confirmation"


def test_record_tenant_outcome_never_raises_on_registry_failure(monkeypatch) -> None:
    """A registry write failure must not propagate -- it's exception-guarded so a
    tenant-accounting bug can never crash an apply run."""

    def boom(conn, host, *, ok, result):
        raise RuntimeError("registry unreachable")

    monkeypatch.setattr(launcher.tenants_mod, "record_submit", boom)

    # Must not raise.
    record_tenant_outcome(object(), "https://boards.greenhouse.io/acme/jobs/123", "applied")


# ---------------------------------------------------------------------------
# run_job wiring: supervised calls the accounting helper exactly once with the
# REAL terminal status; non-supervised never calls it at all.
# ---------------------------------------------------------------------------

def _job(url="https://boards.greenhouse.io/acme/jobs/123"):
    return {
        "url": url,
        "application_url": url,
        "title": "Test Role",
        "site": "acme",
        "tailored_resume_path": None,
        "fit_score": 8,
    }


def test_run_job_supervised_calls_record_once_on_applied(monkeypatch) -> None:
    calls = []

    def fake_record(conn, apply_url, status):
        calls.append((apply_url, status))

    monkeypatch.setattr(launcher, "record_tenant_outcome", fake_record)
    monkeypatch.setattr(launcher, "_run_job_impl",
                         lambda *a, **kw: ("applied", 1234))
    monkeypatch.setattr(launcher, "get_connection", lambda: object())

    status, duration_ms = launcher.run_job(_job(), port=9001, supervised=True)

    assert status == "applied"
    assert duration_ms == 1234
    assert len(calls) == 1
    assert calls[0][1] == "applied"


def test_run_job_supervised_calls_record_once_on_failure(monkeypatch) -> None:
    calls = []

    def fake_record(conn, apply_url, status):
        calls.append((apply_url, status))

    monkeypatch.setattr(launcher, "record_tenant_outcome", fake_record)
    monkeypatch.setattr(launcher, "_run_job_impl",
                         lambda *a, **kw: ("failed:no_confirmation", 987))
    monkeypatch.setattr(launcher, "get_connection", lambda: object())

    status, duration_ms = launcher.run_job(_job(), port=9001, supervised=True)

    assert status == "failed:no_confirmation"
    assert len(calls) == 1
    assert calls[0][1] == "failed:no_confirmation"


def test_run_job_non_supervised_never_calls_record(monkeypatch) -> None:
    calls = []

    def fake_record(conn, apply_url, status):
        calls.append((apply_url, status))

    monkeypatch.setattr(launcher, "record_tenant_outcome", fake_record)
    monkeypatch.setattr(launcher, "_run_job_impl",
                         lambda *a, **kw: ("applied", 1234))
    monkeypatch.setattr(launcher, "get_connection", lambda: object())

    status, duration_ms = launcher.run_job(_job(), port=9001, supervised=False)

    assert status == "applied"
    assert calls == []
