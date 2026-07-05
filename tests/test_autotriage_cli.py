from __future__ import annotations

from applypilot.fleet import autotriage_main


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_autotriage_cli_once_runs_one_pass(monkeypatch, capsys):
    monkeypatch.setattr(autotriage_main.pgqueue, "connect", lambda dsn: _FakeConn())

    calls = []

    def fake_run_pass(conn, **kwargs):
        calls.append(kwargs)
        return {
            "contexts": 2,
            "applied": 1,
            "actions": {"requeue_usage_limit": 1, "no_action": 1},
            "statuses": {"applied": 1, "no_action": 1},
        }

    monkeypatch.setattr(autotriage_main.autotriage, "run_pass", fake_run_pass)

    rc = autotriage_main.main(["--once", "--dsn", "postgresql://x", "--enable-llm", "--limit", "2"])

    assert rc == 0
    assert calls == [
        {
            "brain_path": None,
            "limit": 2,
            "window_minutes": 1440,
            "enable_llm": True,
            "dry_run": False,
        }
    ]
    assert "contexts=2 applied=1" in capsys.readouterr().out
