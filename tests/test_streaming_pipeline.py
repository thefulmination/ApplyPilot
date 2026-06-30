from __future__ import annotations

import threading


def test_streaming_stage_terminates_when_runner_makes_no_progress(monkeypatch) -> None:
    """A downstream streaming stage must not busy-loop forever when its pending
    counter never reaches zero (e.g. it counts rows the runner intentionally
    skips). With upstream done, the stage should run a pass, see no progress,
    and terminate.
    """
    from applypilot import pipeline

    calls = {"n": 0}

    def fake_runner(**kwargs) -> dict:
        calls["n"] += 1
        return {"status": "ok"}

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "enrich", fake_runner)
    # Pending never drops to zero -> simulates un-processable / skipped rows.
    monkeypatch.setattr(pipeline, "_count_pending", lambda stage, min_score=7: 5)
    # Keep the back-off short so the test is fast even if it does wait.
    monkeypatch.setattr(pipeline, "_STREAM_POLL_INTERVAL", 0.01)

    tracker = pipeline._StageTracker()
    tracker.mark_done("discover")  # upstream finished
    stop_event = threading.Event()

    done = threading.Event()

    def run() -> None:
        pipeline._run_stage_streaming("enrich", tracker, stop_event)
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()

    assert done.wait(timeout=5), "streaming stage did not terminate (busy-loop regression)"
    # One pass attempted, then no-progress + upstream-done -> break.
    assert calls["n"] == 1


def test_streaming_stage_processes_until_backlog_drains(monkeypatch) -> None:
    """When passes actually reduce the backlog, the stage keeps running until
    pending hits zero, then stops."""
    from applypilot import pipeline

    pending = {"left": 3}

    def fake_runner(**kwargs) -> dict:
        if pending["left"] > 0:
            pending["left"] -= 1
        return {"status": "ok"}

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "enrich", fake_runner)
    monkeypatch.setattr(pipeline, "_count_pending", lambda stage, min_score=7: pending["left"])
    monkeypatch.setattr(pipeline, "_STREAM_POLL_INTERVAL", 0.01)

    tracker = pipeline._StageTracker()
    tracker.mark_done("discover")
    stop_event = threading.Event()

    done = threading.Event()

    def run() -> None:
        pipeline._run_stage_streaming("enrich", tracker, stop_event)
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(timeout=5)
    assert pending["left"] == 0
