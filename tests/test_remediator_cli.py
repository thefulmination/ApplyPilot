from applypilot.fleet import remediator_main, remediator


class _Conn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_cli_once_runs_remediate_and_prints(monkeypatch, capsys):
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn=None: _Conn())
    monkeypatch.setattr(remediator, "remediate",
                        lambda conn, **k: {"candidates": 3, "requeued": 2,
                                           "vetoed_applied_set": 1, "vetoed_email": 0, "capped": 0})
    rc = remediator_main.main(["--once", "--dsn", "x"])
    assert rc == 0
    assert "requeued" in capsys.readouterr().out.lower()


def test_cli_usage_limit_backfill_passes_backfill_true_to_remediate(monkeypatch, capsys):
    """Phase 2.4 / C12: --usage-limit-backfill must call remediate(..., backfill=True) --
    the status-keyed, no-time-window selection -- instead of the windowed default."""
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn=None: _Conn())
    seen_kwargs = {}

    def _fake_remediate(conn, **k):
        seen_kwargs.update(k)
        return {"candidates": 1, "requeued": 1, "vetoed_applied_set": 0,
                "vetoed_email": 0, "capped": 0}

    monkeypatch.setattr(remediator, "remediate", _fake_remediate)
    rc = remediator_main.main(["--once", "--dsn", "x", "--usage-limit-backfill"])
    assert rc == 0
    assert seen_kwargs.get("backfill") is True


def test_cli_without_backfill_flag_defaults_to_windowed_mode(monkeypatch, capsys):
    """Regression: the existing windowed path must be unchanged -- backfill defaults False."""
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn=None: _Conn())
    seen_kwargs = {}

    def _fake_remediate(conn, **k):
        seen_kwargs.update(k)
        return {"candidates": 0, "requeued": 0, "vetoed_applied_set": 0,
                "vetoed_email": 0, "capped": 0}

    monkeypatch.setattr(remediator, "remediate", _fake_remediate)
    rc = remediator_main.main(["--once", "--dsn", "x"])
    assert rc == 0
    assert seen_kwargs.get("backfill") is False
