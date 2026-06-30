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
