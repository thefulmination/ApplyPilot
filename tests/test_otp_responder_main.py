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


def test_once_failure_records_error_heartbeat_and_returns_nonzero(monkeypatch):
    beats = []

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(pgqueue, "connect", lambda _dsn: _Conn())
    schema_calls = []
    monkeypatch.setattr(
        otp_responder_main.fleet_schema,
        "ensure_schema_v3",
        lambda conn: schema_calls.append(conn),
    )
    monkeypatch.setattr(
        otp_responder_main,
        "run_once",
        lambda _conn: (_ for _ in ()).throw(
            otp_responder_main.otp_relay.OtpResponderOverloadError("overflow")
        ),
    )
    monkeypatch.setattr(
        otp_responder_main,
        "_beat",
        lambda _conn, **kwargs: beats.append(kwargs),
    )

    assert otp_responder_main.main(["--dsn", "test", "--once"]) == 1
    assert len(schema_calls) == 1
    assert [beat["state"] for beat in beats] == ["busy", "error"]
    assert beats[-1]["last_error"] == (
        "otp_responder_cycle_failed:OtpResponderOverloadError"
    )
