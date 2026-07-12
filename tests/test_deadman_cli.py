from __future__ import annotations

from applypilot.apply import pgqueue
from applypilot.fleet import deadman, schema as fleet_schema


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_deadman_scheduled_check_does_not_run_schema_migrations(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(pgqueue, "connect", lambda dsn: _Conn())
    monkeypatch.setattr(deadman, "mail_source_alive", lambda: True)
    monkeypatch.setattr(deadman, "run_deadman", lambda *args, **kwargs: [])
    monkeypatch.setattr(fleet_schema, "ensure_schema_v3", lambda conn: calls.append(conn))

    assert deadman.main(["--dsn", "postgresql://test"]) == 0
    assert calls == []
    assert "healthy" in capsys.readouterr().out

    assert deadman.main(["--dsn", "postgresql://test", "--ensure-schema"]) == 0
    assert len(calls) == 1


def test_deadman_failure_returns_nonzero_and_writes_fallback(
    monkeypatch, tmp_path, capsys,
):
    def fail_connect(_dsn):
        raise RuntimeError("postgresql://user:secret@example.invalid/db")

    monkeypatch.setattr(pgqueue, "connect", fail_connect)

    assert deadman.main([
        "--dsn", "postgresql://test", "--alert-dir", str(tmp_path),
    ]) == 2
    alert = (tmp_path / deadman.ALERT_FILENAME).read_text(encoding="utf-8")
    assert "monitor_failure" in alert
    assert "secret" not in alert
    assert "postgresql" not in alert
    output = capsys.readouterr().out
    assert "secret" not in output
    assert "postgresql" not in output
