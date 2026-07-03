from __future__ import annotations

from applypilot.apply.launcher import resolve_supervised_confirm


def test_confirm_yes_submits() -> None:
    assert resolve_supervised_confirm(True, "y") == (True, "")


def test_confirm_no_abandons() -> None:
    assert resolve_supervised_confirm(True, "n") == (False, "abandoned")


def test_confirm_skip_with_reason() -> None:
    assert resolve_supervised_confirm(True, "skip: bad fit") == (False, "bad fit")


def test_confirm_no_with_reason() -> None:
    assert resolve_supervised_confirm(True, "n: wrong company") == (False, "wrong company")


def test_confirm_no_sentinel_seen() -> None:
    assert resolve_supervised_confirm(False, "y") == (False, "no confirm sentinel")
    assert resolve_supervised_confirm(False, "") == (False, "no confirm sentinel")


def test_confirm_case_and_whitespace_insensitive() -> None:
    assert resolve_supervised_confirm(True, "  Y  ") == (True, "")
    assert resolve_supervised_confirm(True, "N") == (False, "abandoned")
    assert resolve_supervised_confirm(True, "SKIP: Bad Fit") == (False, "Bad Fit")


def test_supervised_confirm_records_submit_ok_true(monkeypatch) -> None:
    """When the owner approves, record_submit must be called with ok=True."""
    from applypilot.apply import launcher

    calls = []

    def fake_record_submit(conn, host, *, ok, result):
        calls.append({"conn": conn, "host": host, "ok": ok, "result": result})

    monkeypatch.setattr(launcher.tenants_mod, "record_submit", fake_record_submit)

    submit, reason = resolve_supervised_confirm(True, "y")
    assert submit is True

    conn = object()
    launcher.tenants_mod.record_submit(
        conn, "example.workday.com", ok=submit, result=reason or None
    )

    assert calls == [
        {"conn": conn, "host": "example.workday.com", "ok": True, "result": None}
    ]


def test_supervised_confirm_records_submit_ok_false(monkeypatch) -> None:
    """When the owner declines, record_submit must be called with ok=False and the reason."""
    from applypilot.apply import launcher

    calls = []

    def fake_record_submit(conn, host, *, ok, result):
        calls.append({"conn": conn, "host": host, "ok": ok, "result": result})

    monkeypatch.setattr(launcher.tenants_mod, "record_submit", fake_record_submit)

    submit, reason = resolve_supervised_confirm(True, "n")
    assert submit is False

    conn = object()
    launcher.tenants_mod.record_submit(
        conn, "example.workday.com", ok=submit, result=reason or None
    )

    assert calls == [
        {"conn": conn, "host": "example.workday.com", "ok": False, "result": "abandoned"}
    ]


def test_supervised_confirm_no_sentinel_records_submit_ok_false(monkeypatch) -> None:
    """No sentinel seen -> never submit; record_submit gets ok=False with the reason."""
    from applypilot.apply import launcher

    calls = []

    def fake_record_submit(conn, host, *, ok, result):
        calls.append({"conn": conn, "host": host, "ok": ok, "result": result})

    monkeypatch.setattr(launcher.tenants_mod, "record_submit", fake_record_submit)

    submit, reason = resolve_supervised_confirm(False, "y")
    assert submit is False

    conn = object()
    launcher.tenants_mod.record_submit(
        conn, "example.workday.com", ok=submit, result=reason or None
    )

    assert calls == [
        {"conn": conn, "host": "example.workday.com", "ok": False, "result": "no confirm sentinel"}
    ]
