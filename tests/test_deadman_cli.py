from __future__ import annotations

from applypilot.apply import pgqueue
from applypilot.fleet import deadman


def test_deadman_failure_returns_nonzero_and_writes_sanitized_fallback(
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
