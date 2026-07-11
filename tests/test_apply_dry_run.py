from __future__ import annotations

import pytest


def _patch_worker_io(monkeypatch, launcher, run_job_result, jobs):
    """Stub out the dashboard, Chrome, and DB-touching bits of worker_loop so the
    result-handling branch can be exercised in isolation. Returns a dict that
    records mark_result / release_lock calls.
    """
    calls: dict = {"mark_result": [], "release_lock": []}

    monkeypatch.setattr(launcher, "update_state", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "add_event", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "get_state", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "launch_chrome", lambda *a, **k: object())
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "run_job", lambda *a, **k: run_job_result)
    monkeypatch.setattr(launcher, "_throttle_before_apply", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "_throttle_after_apply", lambda *a, **k: None)
    monkeypatch.setenv("APPLYPILOT_PREFLIGHT_LIVENESS", "0")

    def fake_acquire(**kwargs):
        return jobs.pop(0) if jobs else None

    monkeypatch.setattr(launcher, "acquire_job", fake_acquire)
    monkeypatch.setattr(launcher, "mark_result",
                        lambda *a, **k: calls["mark_result"].append((a, k)))
    monkeypatch.setattr(launcher, "release_lock",
                        lambda url: calls["release_lock"].append(url))
    return calls


def test_dry_run_never_marks_applied(monkeypatch) -> None:
    from applypilot.apply import launcher

    launcher._stop_event.clear()
    url = "https://example.com/job"
    job = {"url": url, "title": "Chief of Staff", "site": "ExampleCo"}
    calls = _patch_worker_io(monkeypatch, launcher, ("dry_run", 1234), [job])

    applied, failed = launcher.worker_loop(worker_id=0, limit=1, min_score=7, dry_run=True)

    assert calls["mark_result"] == []          # nothing persisted to the DB
    assert calls["release_lock"] == [url]      # lease released so it's retryable
    assert applied == 1 and failed == 0


def test_dry_run_guard_ignores_a_stray_applied_result(monkeypatch) -> None:
    # Defense in depth: even if run_job returns "applied" while dry_run is True,
    # worker_loop must not write apply_status='applied'.
    from applypilot.apply import launcher

    launcher._stop_event.clear()
    url = "https://example.com/job2"
    job = {"url": url, "title": "Strategy Lead", "site": "ExampleCo"}
    calls = _patch_worker_io(monkeypatch, launcher, ("applied", 1234), [job])

    applied, failed = launcher.worker_loop(worker_id=0, limit=1, min_score=7, dry_run=True)

    assert calls["mark_result"] == []
    assert calls["release_lock"] == [url]
    assert applied == 1


def test_real_run_marks_applied(monkeypatch) -> None:
    from applypilot.apply import launcher

    launcher._stop_event.clear()
    url = "https://example.com/job3"
    job = {"url": url, "title": "Staff Engineer", "site": "ExampleCo"}
    calls = _patch_worker_io(monkeypatch, launcher, ("applied", 1234), [job])

    applied, failed = launcher.worker_loop(worker_id=0, limit=1, min_score=7, dry_run=False)

    assert len(calls["mark_result"]) == 1
    args, kwargs = calls["mark_result"][0]
    assert args[0] == url and args[1] == "applied"
    assert applied == 1


@pytest.mark.parametrize("cleanup_mode", ["false", "exception"])
def test_real_run_cleanup_uncertain_never_persists_result(
    monkeypatch, tmp_path, cleanup_mode
) -> None:
    from applypilot.apply import launcher
    from applypilot.apply.lifecycle_fault import LifecycleHardFault

    launcher._stop_event.clear()
    url = "https://example.com/job-cleanup"
    job = {"url": url, "title": "Staff Engineer", "site": "ExampleCo"}
    calls = _patch_worker_io(monkeypatch, launcher, ("applied", 1234), [job])
    monkeypatch.setattr(launcher.config, "DB_PATH", tmp_path / "applypilot.db")
    if cleanup_mode == "false":
        monkeypatch.setattr(launcher, "cleanup_worker", lambda *_args, **_kwargs: False)
    else:
        monkeypatch.setattr(
            launcher,
            "cleanup_worker",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
        )

    with pytest.raises(LifecycleHardFault, match="browser cleanup"):
        launcher.worker_loop(worker_id=0, limit=1, min_score=7, dry_run=False)

    assert calls["mark_result"] == []
    assert len(list((tmp_path / "lifecycle-faults").glob("fault-*.json"))) == 1
