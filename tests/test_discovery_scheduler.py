from __future__ import annotations

import time


def test_fast_discovery_mode_keeps_role_hiringcafe_but_disables_company_watchlist() -> None:
    from applypilot import pipeline

    cfg = {
        "hiring_cafe": {"enabled": True, "company_watchlist_enabled": True, "max_pages": 2},
        "corporate_ats": {"enabled": True},
        "workday_workers": 3,
    }

    tasks = pipeline._discover_source_tasks(cfg, workers=6, discover_mode="fast")
    hiring = next(task for task in tasks if task["name"] == "hiringcafe")

    assert hiring["enabled"] is True
    assert hiring["serial"] is False
    assert hiring["cfg"]["hiring_cafe"]["company_watchlist_enabled"] is False
    assert hiring["cfg"]["hiring_cafe"]["max_pages"] == 1


def test_safe_discovery_mode_uses_parallel_safe_sources_and_serial_jobspy() -> None:
    from applypilot import pipeline

    cfg = {
        "discovery": {"source_parallelism": 4},
        "public_boards": {"enabled": True},
        "hiring_cafe": {"enabled": True},
        "corporate_ats": {"enabled": True},
        "smartextract": {"enabled": False},
    }

    tasks = pipeline._discover_source_tasks(cfg, workers=6, discover_mode="safe")

    assert next(task for task in tasks if task["name"] == "jobspy")["serial"] is True
    assert next(task for task in tasks if task["name"] == "public_boards")["serial"] is False
    assert next(task for task in tasks if task["name"] == "corporate_ats")["workers"] == 8
    assert next(task for task in tasks if task["name"] == "workday")["workers"] == 4
    assert next(task for task in tasks if task["name"] == "smartextract")["enabled"] is False


def test_parallel_discovery_scheduler_runs_safe_tasks_concurrently(monkeypatch) -> None:
    from applypilot import pipeline

    events: list[tuple[str, float]] = []

    def fake_task(name: str):
        def runner() -> dict:
            events.append((f"{name}:start", time.perf_counter()))
            time.sleep(0.15)
            events.append((f"{name}:end", time.perf_counter()))
            return {"status": "ok"}

        return runner

    tasks = [
        {"name": "one", "label": "One", "enabled": True, "serial": False, "runner": fake_task("one")},
        {"name": "two", "label": "Two", "enabled": True, "serial": False, "runner": fake_task("two")},
    ]

    monkeypatch.setattr(pipeline, "_discover_source_tasks", lambda _cfg, workers, discover_mode: tasks)

    started = time.perf_counter()
    result = pipeline._run_discover(workers=2, discover_mode="safe", search_cfg={})
    elapsed = time.perf_counter() - started

    assert result == {"one": "ok", "two": "ok"}
    assert elapsed < 0.27
    assert {name for name, _ in events if name.endswith(":start")} == {"one:start", "two:start"}


def test_discovery_scheduler_marks_source_errors_partial(monkeypatch) -> None:
    from applypilot import pipeline

    def broken() -> dict:
        raise RuntimeError("rate limited")

    tasks = [
        {"name": "jobspy", "label": "JobSpy", "enabled": True, "serial": True, "runner": broken},
        {"name": "public_boards", "label": "Public", "enabled": True, "serial": False, "runner": lambda: {"status": "ok"}},
    ]
    monkeypatch.setattr(pipeline, "_discover_source_tasks", lambda _cfg, workers, discover_mode: tasks)

    result = pipeline._run_discover(workers=2, discover_mode="safe", search_cfg={})

    assert result["jobspy"].startswith("error: rate limited")
    assert result["public_boards"] == "ok"


def test_discovery_scheduler_marks_error_counts_partial(monkeypatch) -> None:
    from applypilot import pipeline

    tasks = [
        {
            "name": "hiringcafe",
            "label": "HiringCafe",
            "enabled": True,
            "serial": False,
            "runner": lambda: {"errors": 2, "new": 10},
        },
    ]
    monkeypatch.setattr(pipeline, "_discover_source_tasks", lambda _cfg, workers, discover_mode: tasks)

    result = pipeline._run_discover(workers=1, discover_mode="safe", search_cfg={})

    assert result["hiringcafe"] == "partial"
