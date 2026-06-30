from applypilot.fleet import diagnoser_main, diagnoser

class _Conn:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self):
        class C:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, *a): s.rows = [{"worker_id": "m2-3"}]
            def fetchall(s): return getattr(s, "rows", [])
        return C()

def test_cli_diagnoses_named_worker(monkeypatch, capsys):
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn=None: _Conn())
    monkeypatch.setattr(diagnoser, "load_worker_ctx",
                        lambda conn, w: diagnoser.WorkerCtx(w, recent_log="x"))
    monkeypatch.setattr(diagnoser, "diagnose",
                        lambda ctx, client=None: diagnoser.Diagnosis(ctx.worker_id, "bot_detected", 0.7, "back off", "deepseek"))
    written = []
    monkeypatch.setattr(diagnoser, "write_diagnosis", lambda conn, d, **k: written.append(d) or True)
    rc = diagnoser_main.main(["--worker", "m2-3"])
    assert rc == 0
    assert written and written[0].root_cause == "bot_detected"
    assert "bot_detected" in capsys.readouterr().out
