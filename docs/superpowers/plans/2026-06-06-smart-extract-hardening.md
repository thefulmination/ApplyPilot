# Smart Extract Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Smart Extract faster, more reliable, and less likely to save junk jobs by adding HTTP probing, source health tracking, validation, and clearer summaries.

**Architecture:** Keep the existing `src/applypilot/discovery/smartextract.py` orchestration, but add focused helper functions inside that module for health, probing, and validation. Add tests in a new `tests/test_smartextract_hardening.py` file using monkeypatch/temp paths so no real network or browser calls are required.

**Tech Stack:** Python 3.11, requests, BeautifulSoup, Playwright, pytest, SQLite-backed ApplyPilot DB.

---

## File Structure

- Modify `src/applypilot/discovery/smartextract.py`
  - Add Smart Extract config values.
  - Add health load/save/update/skip helpers.
  - Add HTTP probe helpers for JSON-LD and static link extraction.
  - Add job validation helpers.
  - Integrate these helpers into `_run_all` and `_run_one_site`.
- Add `tests/test_smartextract_hardening.py`
  - Unit tests for validation, health cooldown, health reset after success, HTTP JSON-LD probing, and summary counts.
- Update `docs/superpowers/plans/2026-06-06-smart-extract-hardening.md`
  - Mark checkboxes as tasks complete during implementation.

---

### Task 1: Add Validation Helpers

**Files:**
- Modify: `src/applypilot/discovery/smartextract.py`
- Test: `tests/test_smartextract_hardening.py`

- [x] **Step 1: Write failing tests for Smart Extract job validation**

Add tests that import `applypilot.discovery.smartextract` and assert:

```python
def test_validate_smart_jobs_rejects_junk_and_relative_urls():
    jobs = [
        {"title": "Chief of Staff", "url": "https://example.com/jobs/1", "location": "Remote"},
        {"title": "Apply", "url": "https://example.com/apply", "location": "Remote"},
        {"title": "Strategy Lead", "url": "/jobs/2", "location": "Remote"},
        {"title": "", "url": "https://example.com/jobs/3", "location": "Remote"},
        {"title": "Chief of Staff", "url": "https://example.com/jobs/1", "location": "Remote"},
    ]

    valid, counts = smartextract.validate_smart_jobs(jobs)

    assert valid == [{"title": "Chief of Staff", "url": "https://example.com/jobs/1", "location": "Remote"}]
    assert counts["accepted"] == 1
    assert counts["invalid_title"] == 2
    assert counts["invalid_url"] == 1
    assert counts["duplicate_url"] == 1
```

- [x] **Step 2: Run test and verify it fails**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_smartextract_hardening.py -q
```

Expected: fails because `validate_smart_jobs` does not exist.

- [x] **Step 3: Implement validation helpers**

Add `validate_smart_jobs(jobs)` and `_is_valid_smart_title(title)` to `smartextract.py`. Validation must require absolute `http` or `https` URLs, reject blank/navigation/action titles, and dedupe URLs within the batch.

- [x] **Step 4: Run validation tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_smartextract_hardening.py -q
```

Expected: validation test passes or only later-task tests fail.

---

### Task 2: Add Source Health Tracking

**Files:**
- Modify: `src/applypilot/discovery/smartextract.py`
- Test: `tests/test_smartextract_hardening.py`

- [x] **Step 1: Write failing health tests**

Add tests that monkeypatch `smartextract.SMART_HEALTH_PATH` to a temp file and assert:

```python
def test_health_cooldown_skips_after_repeated_failures(tmp_path, monkeypatch):
    health_path = tmp_path / "smartextract_health.json"
    monkeypatch.setattr(smartextract, "SMART_HEALTH_PATH", health_path)
    health = {}
    for _ in range(3):
        smartextract.record_source_health(
            health,
            "SlowBoard",
            url="https://example.com/jobs",
            status="COLLECT_ERROR",
            strategy=None,
            jobs_found=0,
            elapsed_seconds=30.0,
            hard_failure=True,
            timeout=True,
        )

    should_skip, reason = smartextract.should_skip_source(
        health,
        "SlowBoard",
        skip_after_failures=3,
        cooldown_hours=24,
    )

    assert should_skip
    assert "cooldown" in reason
```

Also add:

```python
def test_health_success_resets_consecutive_failures(tmp_path, monkeypatch):
    health_path = tmp_path / "smartextract_health.json"
    monkeypatch.setattr(smartextract, "SMART_HEALTH_PATH", health_path)
    health = {}
    smartextract.record_source_health(health, "Board", url="u", status="FAIL", strategy=None, jobs_found=0, elapsed_seconds=1, hard_failure=True)
    smartextract.record_source_health(health, "Board", url="u", status="PASS", strategy="http_probe", jobs_found=2, elapsed_seconds=1, hard_failure=False)

    assert health["Board"]["consecutive_failures"] == 0
    assert health["Board"]["last_successful_strategy"] == "http_probe"
```

- [x] **Step 2: Run tests and verify they fail**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_smartextract_hardening.py -q
```

Expected: fails because health functions do not exist.

- [x] **Step 3: Implement health helpers**

Add `SMART_HEALTH_PATH = config.APP_DIR / "smartextract_health.json"`, `load_smart_health()`, `save_smart_health(health)`, `record_source_health(...)`, and `should_skip_source(...)`. Use ISO timestamps and average runtime smoothing.

- [x] **Step 4: Run health tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_smartextract_hardening.py -q
```

Expected: validation and health tests pass or only later-task tests fail.

---

### Task 3: Add HTTP Probe

**Files:**
- Modify: `src/applypilot/discovery/smartextract.py`
- Test: `tests/test_smartextract_hardening.py`

- [x] **Step 1: Write failing HTTP probe test**

Add a test that monkeypatches `requests.get` to return HTML with JSON-LD:

```python
def test_http_probe_extracts_json_ld_job(monkeypatch):
    html = '''
    <html><head>
      <script type="application/ld+json">
      {"@type":"JobPosting","title":"Chief of Staff","description":"Run operations","url":"https://example.com/jobs/chief","jobLocation":{"address":{"addressLocality":"New York"}}}
      </script>
    </head></html>
    '''

    class Response:
        text = html
        status_code = 200
        headers = {"content-type": "text/html"}
        url = "https://example.com/jobs"
        def raise_for_status(self):
            return None

    monkeypatch.setattr(smartextract.requests, "get", lambda *args, **kwargs: Response())

    result = smartextract.http_probe_target("Example", "https://example.com/jobs")

    assert result["status"] == "PASS"
    assert result["strategy"] == "http_probe"
    assert result["jobs"][0]["title"] == "Chief of Staff"
    assert result["jobs"][0]["url"] == "https://example.com/jobs/chief"
```

- [x] **Step 2: Run test and verify it fails**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_smartextract_hardening.py -q
```

Expected: fails because `requests` and `http_probe_target` are not available in `smartextract.py`.

- [x] **Step 3: Implement HTTP probe**

Import `requests` and `urljoin`. Add `http_probe_target(name, url)` that fetches HTML with the existing user agent, extracts JSON-LD `JobPosting` entries, normalizes title/description/location/url, validates jobs, and returns a Smart Extract-style result dict. If there are no valid jobs, return `{"status": "FAIL", "strategy": "http_probe", "jobs": [], ...}` without raising.

- [x] **Step 4: Run HTTP probe tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_smartextract_hardening.py -q
```

Expected: tests pass or only integration-summary tests fail.

---

### Task 4: Integrate Probe, Health, And Summary

**Files:**
- Modify: `src/applypilot/discovery/smartextract.py`
- Test: `tests/test_smartextract_hardening.py`

- [x] **Step 1: Write integration-style unit test for skipped source summary**

Add a test that calls `should_skip_source` and verifies skipped counts are represented by a result dict from a new helper:

```python
def test_skip_result_shape_contains_reason():
    result = smartextract.make_skip_result("SlowBoard", "health cooldown: 3 failures")

    assert result["name"] == "SlowBoard"
    assert result["status"] == "SKIPPED"
    assert result["skip_reason"] == "health cooldown: 3 failures"
    assert result["jobs"] == []
```

- [x] **Step 2: Run test and verify it fails**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_smartextract_hardening.py -q
```

Expected: fails because `make_skip_result` does not exist.

- [x] **Step 3: Implement integration**

Update `_apply_smart_config` to read:

- `http_probe_enabled`
- `health_enabled`
- `skip_after_failures`
- `skip_cooldown_hours`
- `min_valid_jobs_for_success`

Update `_run_all` so each target:

- checks source health before work
- optionally runs `http_probe_target`
- falls back to `_run_one_site` if the probe returns no valid jobs
- validates browser/LLM jobs before storage
- records health after each target
- logs skipped and validation counts in the summary

- [x] **Step 4: Run focused tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_smartextract_hardening.py -q
```

Expected: all hardening tests pass.

---

### Task 5: Full Verification And Commit

**Files:**
- Modify: `src/applypilot/discovery/smartextract.py`
- Add: `tests/test_smartextract_hardening.py`
- Update: `docs/superpowers/plans/2026-06-06-smart-extract-hardening.md`

- [x] **Step 1: Run full tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest
```

Expected: all tests pass.

- [x] **Step 2: Run lint**

Run:

```powershell
.\.conda-env\python.exe -m ruff check src tests
```

Expected: all checks pass.

- [x] **Step 3: Run compile check**

Run:

```powershell
.\.conda-env\python.exe -m compileall -q src tests
```

Expected: exit code 0.

- [x] **Step 4: Run doctor**

Run:

```powershell
.\run-applypilot.ps1 doctor
```

Expected: LLM provider and Chrome remain OK.

- [x] **Step 5: Commit code changes**

Run:

```powershell
git add src/applypilot/discovery/smartextract.py tests/test_smartextract_hardening.py docs/superpowers/plans/2026-06-06-smart-extract-hardening.md
git commit -m "Harden Smart Extract discovery"
```

Expected: commit succeeds.
