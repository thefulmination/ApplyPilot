import json

from applypilot.fleet import email_reconcile as er
from applypilot.fleet import email_reconcile_main


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _resolution(url="u1", classification="confirmed"):
    return er.Resolution(
        job_url=url,
        message_id=f"m-{url}",
        method="company_domain",
        score=1.0,
        stage="acknowledged",
        occurred_at="2026-07-01T00:00:00+00:00",
        classification=classification,
    )


def test_cli_json_dry_run_passes_limit(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(email_reconcile_main.sqlite3, "connect", lambda *a, **kw: _Conn())
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn=None: _Conn())
    monkeypatch.setattr(er, "load_outcome_emails", lambda home: ["email"])

    def fake_load_crash_jobs(conn, *, limit=None):
        seen["limit"] = limit
        return [{"url": "u1"}]

    monkeypatch.setattr(er, "load_crash_jobs", fake_load_crash_jobs)
    monkeypatch.setattr(
        er,
        "reconcile",
        lambda emails, jobs, min_strong=er.MIN_STRONG: er.ReconcileResult(
            confirmed=[_resolution("u1")], probable=[_resolution("u2", "probable")],
            unmatched_emails=2, jobs_total=1,
        ),
    )

    rc = email_reconcile_main.main(
        ["--dsn", "pg", "--home-db", "home.db", "--no-scan", "--json", "--limit", "5"]
    )

    assert rc == 0
    assert seen["limit"] == 5
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["jobs_total"] == 1
    assert payload["confirmed"] == 1
    assert payload["probable"] == 1


def test_cli_apply_confirmed_only_max_flips(monkeypatch, capsys):
    applied = []
    monkeypatch.setattr(email_reconcile_main.sqlite3, "connect", lambda *a, **kw: _Conn())
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn=None: _Conn())
    monkeypatch.setattr(er, "load_outcome_emails", lambda home: ["email"])
    monkeypatch.setattr(er, "load_crash_jobs", lambda conn, *, limit=None: [{"url": "u1"}])
    monkeypatch.setattr(
        er,
        "reconcile",
        lambda emails, jobs, min_strong=er.MIN_STRONG: er.ReconcileResult(
            confirmed=[_resolution("u1"), _resolution("u2")],
            probable=[_resolution("u3", "probable")],
            unmatched_emails=0,
            jobs_total=3,
        ),
    )

    def fake_apply(conn, result, *, include_probable=False, max_flips=None):
        applied.append((len(result.confirmed), len(result.probable), include_probable, max_flips))
        return {"flipped": max_flips, "skipped": 0}

    monkeypatch.setattr(er, "apply_resolutions", fake_apply)

    rc = email_reconcile_main.main(
        [
            "--dsn",
            "pg",
            "--home-db",
            "home.db",
            "--no-scan",
            "--apply",
            "--confirmed-only",
            "--max-flips",
            "1",
            "--json",
        ]
    )

    assert rc == 0
    assert applied == [(2, 1, False, 1)]
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] == {"flipped": 1, "skipped": 0}


def test_cli_rejects_confirmed_only_with_apply_probable(capsys):
    rc = email_reconcile_main.main(["--confirmed-only", "--apply-probable"])

    assert rc == 2
    assert "cannot combine" in capsys.readouterr().err.lower()
