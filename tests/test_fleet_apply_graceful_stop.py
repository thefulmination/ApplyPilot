"""SIGTERM graceful stop: the launchd/wrapper update path sends SIGTERM; the worker must
finish the CURRENT job and exit before the next lease (a mid-apply kill parks the job
crash_unconfirmed = the 'may-have-submitted' double-apply vector)."""
from unittest.mock import MagicMock

import pytest

from applypilot.fleet import apply_worker_main as awm

pytestmark = pytest.mark.usefixtures("acquisition_admitted")


class _StubCtx:
    def __enter__(self):
        return MagicMock()

    def __exit__(self, *a):
        return False


def _conn_factory():
    return _StubCtx()


def test_stop_flag_set_by_handler():
    awm._STOP_REQUESTED.clear()
    assert not awm.stop_requested()
    awm.request_stop()  # exactly what the signal handler invokes
    assert awm.stop_requested()
    awm._STOP_REQUESTED.clear()


def test_run_apply_finishes_current_job_then_exits(monkeypatch):
    awm._STOP_REQUESTED.clear()
    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: False)
    calls = {"n": 0}
    loop = MagicMock()

    def run_once():
        calls["n"] += 1
        awm.request_stop()  # SIGTERM lands mid-job
        return {"action": "applied"}

    loop.run_once = run_once
    counts = awm.run_apply(_conn_factory, loop, max_iterations=None, idle_sleep=0)
    assert calls["n"] == 1  # current job completed; NO second lease
    assert counts["applied"] == 1
    awm._STOP_REQUESTED.clear()


def test_run_apply_exits_immediately_if_stop_already_requested(monkeypatch):
    awm._STOP_REQUESTED.clear()
    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: False)
    loop = MagicMock()
    awm.request_stop()
    counts = awm.run_apply(_conn_factory, loop, max_iterations=None, idle_sleep=0)
    loop.run_once.assert_not_called()
    assert counts == {"applied": 0, "halted": 0, "idle": 0, "error": 0}
    awm._STOP_REQUESTED.clear()


def test_run_apply_beats_while_halted(monkeypatch):
    # A halted (ats_paused / canary-exhausted) worker must still emit a heartbeat, else a
    # correctly-PAUSED fleet is indistinguishable from a DEAD one -- live 2026-07-03: paused
    # workers went stale, looked crashed to the watchdog and the console.
    awm._STOP_REQUESTED.clear()
    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: True)
    loop = MagicMock()
    counts = awm.run_apply(_conn_factory, loop, max_iterations=3, idle_sleep=0)
    assert counts["halted"] == 3
    loop.run_once.assert_not_called()                     # halted -> never leases a job
    assert loop._beat.call_count == 3                     # ...but beats every tick
    assert all(c.kwargs.get("state") == "paused" for c in loop._beat.call_args_list)
    awm._STOP_REQUESTED.clear()


def test_stop_handler_survives_launcher_import():
    """launcher.py installs its own SIGTERM handler at import time (non-Windows).
    install_stop_handler must be called AFTER that import (main() does) so the
    graceful handler is the last writer; this pins getsignal to request_stop."""
    import signal

    from applypilot.apply import launcher  # noqa: F401 - the handler-installing import

    awm.install_stop_handler()
    assert signal.getsignal(signal.SIGTERM) is awm.request_stop
