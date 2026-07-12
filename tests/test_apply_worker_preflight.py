from applypilot.apply import liveness, pgqueue
from applypilot.fleet import apply_worker_main as worker_main
from applypilot.fleet import worker as fleet_worker


def test_build_apply_loop_wires_exact_liveness_probe(monkeypatch):
    captured = {}

    class FakeLoop:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    apply_fn = object()
    log_tail_fn = object()
    monkeypatch.setattr(worker_main, "_setup_apply_env", lambda: None)
    monkeypatch.setattr(worker_main, "_apply_timeout_override", lambda dsn: None)
    monkeypatch.setattr(worker_main, "make_apply_fn", lambda *args, **kwargs: apply_fn)
    monkeypatch.setattr(worker_main, "make_log_tail_fn", lambda slot: log_tail_fn)
    monkeypatch.setattr(fleet_worker, "WorkerLoop", FakeLoop)
    monkeypatch.setattr(
        pgqueue,
        "connect",
        lambda dsn: (_ for _ in ()).throw(AssertionError("database connection attempted")),
    )

    worker_main.build_apply_loop(
        dsn="postgresql://fleet",
        worker_id="apply-home-2",
        home_ip="10.0.0.5",
        machine_owner="home",
    )

    assert captured["preflight_fn"] is liveness.probe_url
