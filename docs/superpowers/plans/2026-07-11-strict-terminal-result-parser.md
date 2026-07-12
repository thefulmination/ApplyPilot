# Strict Terminal Result Parser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make application outcomes depend only on the last standalone terminal `RESULT:` line, preventing narrative mentions from creating false applied records or prematurely killing agent streams.

**Architecture:** Add a pure parser in `launcher.py` that extracts strict terminal lines and returns the existing normalized run status. Use the same helper for early terminal detection, final-message selection, and final status routing so those boundaries cannot disagree. Preserve the current no-result and usage-limit handling when no strict terminal line exists.

**Tech Stack:** Python 3, regular expressions, pytest

---

### Task 1: Add Strict Parser Regression Tests

**Files:**
- Create: `tests/test_apply_terminal_result_parser.py`
- Modify: `src/applypilot/apply/launcher.py:1681-1683`
- Modify: `src/applypilot/apply/launcher.py:1853-1939`

- [ ] **Step 1: Write focused failing tests**

Create `tests/test_apply_terminal_result_parser.py`:

```python
from applypilot.apply.launcher import _parse_terminal_result


def test_narrative_applied_mention_does_not_override_later_failure():
    text = """Given the hard rule to never claim RESULT:APPLIED without observed
confirmation, I am stopping here.

RESULT:FAILED:budget_exhausted
"""
    assert _parse_terminal_result(text) == "failed:budget_exhausted"


def test_only_standalone_result_lines_are_terminal():
    assert _parse_terminal_result("Never claim RESULT:APPLIED without proof.") is None


def test_last_standalone_terminal_line_wins():
    text = "RESULT:FAILED:validation\nRESULT:APPLIED\n"
    assert _parse_terminal_result(text) == "applied"


def test_markdown_wrapped_terminal_line_is_supported():
    assert _parse_terminal_result("**RESULT:EXPIRED**") == "expired"


def test_failed_reason_is_cleaned_and_normalized():
    assert _parse_terminal_result("`RESULT:FAILED:No_Confirmation`") == "failed:no_confirmation"


def test_known_non_failure_statuses_are_normalized():
    assert _parse_terminal_result("RESULT:DRY_RUN") == "dry_run"
    assert _parse_terminal_result("RESULT:AUTH_REQUIRED") == "auth_required"


def test_unknown_result_code_is_not_terminal():
    assert _parse_terminal_result("RESULT:MAYBE") is None
```

- [ ] **Step 2: Run the tests and verify the intended failure**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_apply_terminal_result_parser.py -q
```

Expected: collection fails because `_parse_terminal_result` does not exist.

### Task 2: Implement and Integrate the Parser

**Files:**
- Modify: `src/applypilot/apply/launcher.py`
- Test: `tests/test_apply_terminal_result_parser.py`
- Test: `tests/test_apply_timeout_stats.py`

- [ ] **Step 1: Add the pure parser**

Add near the existing no-result helpers:

```python
_TERMINAL_RESULT_RE = re.compile(
    r"(?im)^\\s*[*`\"]*RESULT:(APPLIED|EXPIRED|CAPTCHA|LOGIN_ISSUE|"
    r"AUTH_REQUIRED|DRY_RUN|FAILED(?::[^\\r\\n*`\"]+)?)"
    r"[*`\"]*\\s*$"
)


def _parse_terminal_result(text: str | None) -> str | None:
    if not text:
        return None
    matches = list(_TERMINAL_RESULT_RE.finditer(text))
    if not matches:
        return None
    code = matches[-1].group(1).strip()
    upper = code.upper()
    if upper.startswith("FAILED"):
        reason = code.split(":", 1)[1].strip() if ":" in code else "unknown"
        reason = re.sub(r'[*`\"]+$', '', reason).strip().lower()
        return f"failed:{reason or 'unknown'}"
    return upper.lower()
```

- [ ] **Step 2: Use the helper for stream termination**

Change `_note_terminal_result` so it sets the event only when `_parse_terminal_result(text)` returns a status. A narrative mention must not stop the reader thread.

- [ ] **Step 3: Use one parsed status for final routing**

Parse `final_text` first. When it has no strict terminal line, parse the complete transcript. Set `final_result_source` from the source that actually produced the parsed status. Replace substring status checks with branching on the normalized parsed status. Keep `_no_result_status()` as the fallback when parsed status is `None`.

- [ ] **Step 4: Run focused tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_apply_terminal_result_parser.py tests\test_apply_timeout_stats.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Run adjacent launcher and fleet tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_worker_usage_limit.py tests\test_fleet_apply_lane.py tests\test_fleet_v3_worker.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Verify the historical NVIDIA transcript**

Run `_parse_terminal_result()` against the linked historical log text and require `failed:budget_exhausted`. This check is read-only and must not mutate historical rows; corrections belong to item 2.
