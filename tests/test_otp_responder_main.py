"""The responder entrypoint answers + purges in one cycle and is import-safe."""
from applypilot.apply import pgqueue
from applypilot.fleet import otp_responder_main, schema as fleet_schema


def test_run_once_answers_and_purges(fleet_db, monkeypatch):
    calls = {"answer": 0, "purge": 0}
    monkeypatch.setattr(otp_responder_main.otp_relay, "answer_pending",
                        lambda conn, svc, **kw: calls.__setitem__("answer", calls["answer"] + 1) or 3)
    monkeypatch.setattr(otp_responder_main.otp_relay, "purge_expired",
                        lambda conn: calls.__setitem__("purge", calls["purge"] + 1) or 1)
    with pgqueue.connect(fleet_db) as conn:
        fleet_schema.ensure_schema_v3(conn)
        out = otp_responder_main.run_once(conn, gmail_service=object())
    assert out == {"answered": 3, "purged": 1}
    assert calls == {"answer": 1, "purge": 1}


def test_main_requires_dsn(monkeypatch):
    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    try:
        otp_responder_main.main(["--once"])
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "dsn" in str(e).lower()
