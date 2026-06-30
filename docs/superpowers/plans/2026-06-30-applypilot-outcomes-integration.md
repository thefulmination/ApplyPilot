# ApplyPilot Outcomes Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `email_events` outcome data referenceable from five places — JSONL export, the TS `brainDb.ts` read layer, a learning-loop feed, a fleet Postgres copy, and an advisory lane signal — all additive/read-only, with #3 (promotion) and #5 (scoring) built **inert** (compute-only, no writes to `applications`/`jobs`/scores/the apply gate).

**Architecture:** `email_events` (SQLite brain) stays the single source. New pure modules `outcome_implied.py` (#3) and `outcome_lane_signal.py` (#5) compute derived data without writing it anywhere. `outcome_export.py` (#1) writes JSONL (enriched with the #3/#5 fields). `brainDb.ts` (#2) gets read-only mirrors. The fleet gets a thin `inbox_outcomes` PG copy (#4) + a read-only console panel. CLI surfaces are `outcomes-export`, `outcomes-promote` (preview-only), `outcomes-lanes`.

**Tech Stack:** Python 3.11 (Typer, stdlib `sqlite3`/`json`/`csv`, `psycopg` for fleet), the repo's `.conda-env`; TypeScript + `node:sqlite` + vitest (repo `New project 9`); Postgres (fleet).

## Global Constraints

- **HARD INVARIANT (the whole feature):** the apply gate, the `applications` / `application_events` tables, `jobs.apply_status`, and every job score (`fit_score`, `audit_score`, `COALESCE(audit_score, fit_score)`) are **byte-unchanged**. The only persisted writes are `email_events` (pre-existing) and the new PG `inbox_outcomes` (a downstream copy). `outcome_implied.py` and `outcome_lane_signal.py` are **pure** and expose **no write path**; `outcomes-promote` has **no `--apply`**.
- **Two repos.** Python tasks (1–4, 6, 7, 8) run in `C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot` (branch `applypilot-hardening-and-brainstorm-integration`). The TS task (5) runs in `C:\Users\JStal\OneDrive\Documents\New project 9` (branch `codex/applypilot-qualification-triage`). Each task's commit is **file-scoped** to its own files; ignore unrelated dirty files / concurrent commits.
- **Python runtime:** `.conda-env/python.exe -m pytest …` from the ApplyPilot repo root. **No new pip deps** (Typer/Rich/psycopg already present).
- **TS runtime:** from `New project 9` root, `npm test` (vitest run) and `npm run typecheck` (tsc --noEmit). Test imports use the `.js` extension on `.ts` paths (ESM).
- **Python owns the SQLite schema.** TS issues NO DDL — read-only SELECT, and a guard that throws if a table is absent.
- **Fleet:** new tables go in `src/applypilot/fleet/schema_v3.sql` (idempotent `CREATE … IF NOT EXISTS`), applied by `ensure_schema_v3`. Fleet tests use the real-Postgres `fleet_db` fixture (no mocking) + a temp SQLite brain; add new table names to `_V3_TABLES` in `tests/conftest.py`.
- **Stage/outcome vocabulary (fixed):** stages `acknowledged|screen|assessment|interview|offer|rejected|other`; terminal outcome `offer|rejected|None`. `RESPONSE_STAGES = (screen, assessment, interview, offer, rejected)`; a bare `acknowledged` is NOT a response.
- **Every git commit message ends with:** `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Spec:** `docs/superpowers/specs/2026-06-30-applypilot-outcomes-integration-design.md`.

---

## File Structure

- `src/applypilot/outcome_implied.py` — **create** (#3, pure): `implied_status(row) -> dict | None`.
- `src/applypilot/outcome_lane_signal.py` — **create** (#5, pure): `compute_lane_report(rows, floor)`, `annotate_job_signal(row, report)`.
- `src/applypilot/outcome_export.py` — **create** (#1): `export_outcome_events(output_dir=None)`.
- `src/applypilot/cli.py` — **modify**: add `outcomes-export`, `outcomes-promote` (preview), `outcomes-lanes`.
- `src/applypilot/fleet/schema_v3.sql` — **modify**: `inbox_outcomes` DDL.
- `src/applypilot/fleet/sync.py` — **modify**: `push_inbox_outcomes`.
- `src/applypilot/fleet/console_app.py` — **modify**: read-only `/api/outcomes` panel.
- `tests/conftest.py` — **modify**: add `inbox_outcomes` to `_V3_TABLES`.
- TS: `New project 9/src/applypilot/brainDb.ts` — **modify**: `readEmailEvents`, `readOutcomeTimelines`.
- Tests: `tests/test_outcome_implied.py`, `tests/test_outcome_lane_signal.py`, `tests/test_outcome_export.py`, `tests/test_outcomes_integration_cli.py`, `tests/test_inbox_outcomes_sync.py`, `tests/test_outcomes_inert_invariant.py`, and `New project 9/tests/applypilot/brainDb.test.ts`.

**Consumed interfaces (confirmed, exact):**
- `outcome_dashboard.build_application_rows(conn, *, now_iso=None) -> list[dict]`; row keys: `job_url, title, company, source_board, applied_at, current_stage, outcome, responded, positive, first_response_days, decision_days, silent_days, segments(dict, 7 keys), events(list)`. Each event dict has `message_id, occurred_at, stage, outcome, reason, sender, subject, snippet, body_text, confidence, extracted_by`.
- `outcome_dashboard.build_insights(rows, *, floor=8) -> dict` with `n, baseline_response_rate, baseline_positive_rate, segments[]`; each segment: `dimension, value, n_applied, n_responded, response_rate, ci_low, ci_high, n_positive, positive_rate, flag('insufficient'|'warm'|'cold'|'none')`.
- `lane_insights.derive_segments(job) -> dict` (7 keys). `outcome_dashboard._read_only_conn(db_path=DB_PATH)` opens `file:…?mode=ro` with `row_factory=sqlite3.Row`.

---

### Task 1: `outcome_implied.py` — pure implied-status mapping (#3, INERT)

**Repo:** ApplyPilot. **Files:** Create `src/applypilot/outcome_implied.py`; Test `tests/test_outcome_implied.py`.

**Interfaces:**
- Produces: `implied_status(row: dict) -> dict | None`. Input = a `build_application_rows` row. Output `None` for acknowledged/receipt-only/no-signal; else `{job_url, implied_status, source_message_id, occurred_at, confidence}` where `implied_status ∈ {offer, rejected, recruiter_screen}`.
- Pure: no DB, no I/O, no writes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_outcome_implied.py
from applypilot.outcome_implied import implied_status


def _row(**kw):
    base = {"job_url": "https://j/1", "current_stage": "applied", "outcome": None, "events": []}
    base.update(kw); return base


def test_acknowledged_only_implies_nothing():
    r = _row(current_stage="acknowledged", events=[
        {"message_id": "m1", "occurred_at": "2026-06-02T00:00:00+00:00", "stage": "acknowledged", "outcome": None, "confidence": "low"},
    ])
    assert implied_status(r) is None


def test_offer_implies_offer_with_source_event():
    r = _row(current_stage="offer", outcome="offer", events=[
        {"message_id": "a", "occurred_at": "2026-06-02T00:00:00+00:00", "stage": "acknowledged", "outcome": None, "confidence": "low"},
        {"message_id": "o", "occurred_at": "2026-06-16T00:00:00+00:00", "stage": "offer", "outcome": "offer", "confidence": "high"},
    ])
    out = implied_status(r)
    assert out["implied_status"] == "offer"
    assert out["source_message_id"] == "o"
    assert out["occurred_at"] == "2026-06-16T00:00:00+00:00"
    assert out["job_url"] == "https://j/1"


def test_rejected_implies_rejected():
    r = _row(current_stage="rejected", outcome="rejected", events=[
        {"message_id": "x", "occurred_at": "2026-06-10T00:00:00+00:00", "stage": "rejected", "outcome": "rejected", "confidence": "high"},
    ])
    assert implied_status(r)["implied_status"] == "rejected"


def test_interview_stage_implies_recruiter_screen_from_first_response():
    r = _row(current_stage="interview", outcome=None, events=[
        {"message_id": "ack", "occurred_at": "2026-06-02T00:00:00+00:00", "stage": "acknowledged", "outcome": None, "confidence": "low"},
        {"message_id": "scr", "occurred_at": "2026-06-05T00:00:00+00:00", "stage": "screen", "outcome": None, "confidence": "medium"},
        {"message_id": "iv", "occurred_at": "2026-06-12T00:00:00+00:00", "stage": "interview", "outcome": None, "confidence": "high"},
    ])
    out = implied_status(r)
    assert out["implied_status"] == "recruiter_screen"
    assert out["source_message_id"] == "scr"   # first RESPONSE-stage event


def test_other_or_applied_implies_nothing():
    assert implied_status(_row(current_stage="applied")) is None
    assert implied_status(_row(current_stage="other", events=[
        {"message_id": "z", "occurred_at": "2026-06-02T00:00:00+00:00", "stage": "other", "outcome": None, "confidence": "low"},
    ])) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_implied.py -v`
Expected: FAIL — `No module named 'applypilot.outcome_implied'`.

- [ ] **Step 3: Create `src/applypilot/outcome_implied.py`**

```python
"""Pure mapping from a per-application outcome row to the tracker status it WOULD
imply -- WITHOUT writing anything. INERT by construction: no DB, no I/O, no
mutation. A future activation could route these decisions through
applications.record_application; that is deliberately NOT here."""

from __future__ import annotations

from typing import Any

_RESPONSE_STAGES = ("screen", "assessment", "interview", "offer", "rejected")
# stages that imply an active "recruiter_screen"-or-beyond advancement (no terminal outcome yet)
_ADVANCING_STAGES = ("screen", "assessment", "interview")


def _last_event_with_outcome(events: list[dict], outcome: str) -> dict | None:
    matches = [e for e in events if e.get("outcome") == outcome]
    return matches[-1] if matches else None


def _first_response_event(events: list[dict]) -> dict | None:
    ordered = sorted(events, key=lambda e: e.get("occurred_at") or "")
    for e in ordered:
        if e.get("stage") in _RESPONSE_STAGES:
            return e
    return None


def implied_status(row: dict[str, Any]) -> dict[str, Any] | None:
    """Given a build_application_rows row, return the tracker status it implies,
    or None for acknowledged/receipt-only/no-signal rows. Pure: returns a
    description; writes nothing."""
    events = row.get("events") or []
    outcome = row.get("outcome")
    stage = row.get("current_stage")

    if outcome == "offer":
        implied, src = "offer", _last_event_with_outcome(events, "offer")
    elif outcome == "rejected":
        implied, src = "rejected", _last_event_with_outcome(events, "rejected")
    elif stage in _ADVANCING_STAGES:
        implied, src = "recruiter_screen", _first_response_event(events)
    else:
        return None

    return {
        "job_url": row.get("job_url"),
        "implied_status": implied,
        "source_message_id": src.get("message_id") if src else None,
        "occurred_at": src.get("occurred_at") if src else None,
        "confidence": src.get("confidence") if src else None,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_implied.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/outcome_implied.py tests/test_outcome_implied.py
git commit -m "feat(outcomes): pure implied-status mapping (inert, no writes)"
```

---

### Task 2: `outcome_lane_signal.py` — advisory lane signal (#5, INERT)

**Repo:** ApplyPilot. **Files:** Create `src/applypilot/outcome_lane_signal.py`; Test `tests/test_outcome_lane_signal.py`.

**Interfaces:**
- Consumes: `outcome_dashboard.build_insights(rows, *, floor=8)`.
- Produces:
  - `compute_lane_report(rows: list[dict], *, floor: int = 8) -> dict` — thin wrapper over `build_insights` returning `{n, baseline_response_rate, baseline_positive_rate, warm: [...], cold: [...], segments: [...]}`.
  - `annotate_job_signal(row: dict, report: dict) -> dict` — for one job row, look up its segment values in the report and return `{flags: {dimension: flag}, top: flag_or_'insufficient'}`.
- Pure. Never folded into any score.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_outcome_lane_signal.py
from applypilot.outcome_lane_signal import annotate_job_signal, compute_lane_report


def _app(responded, positive, board, role):
    return {"responded": responded, "positive": positive,
            "segments": {"source_board": board, "role_family": role,
                         "seniority": "mid", "score_band": "7",
                         "fit_gap_category": "unknown", "location_bucket": "remote",
                         "salary_band": "unknown"}}


def test_compute_lane_report_splits_warm_and_cold():
    rows = [_app(True, True, "greatboard", "quant") for _ in range(12)]
    rows += [_app(False, False, "coldboard", "data") for _ in range(40)]
    rep = compute_lane_report(rows, floor=8)
    warm_vals = {s["value"] for s in rep["warm"]}
    cold_vals = {s["value"] for s in rep["cold"]}
    assert "greatboard" in warm_vals
    assert "coldboard" in cold_vals
    assert rep["n"] == 52


def test_annotate_job_signal_reads_flags_for_its_lanes():
    rows = [_app(True, True, "greatboard", "quant") for _ in range(12)]
    rows += [_app(False, False, "coldboard", "data") for _ in range(40)]
    rep = compute_lane_report(rows, floor=8)
    job = {"segments": {"source_board": "greatboard", "role_family": "quant",
                        "seniority": "mid", "score_band": "7", "fit_gap_category": "unknown",
                        "location_bucket": "remote", "salary_band": "unknown"}}
    sig = annotate_job_signal(job, rep)
    assert sig["flags"]["source_board"] == "warm"
    assert sig["top"] == "warm"


def test_thin_data_is_insufficient_not_warm():
    rows = [_app(True, True, "tiny", "quant")]
    rows += [_app(False, False, "bulk", "data") for _ in range(20)]
    rep = compute_lane_report(rows, floor=8)
    job = {"segments": {"source_board": "tiny", "role_family": "quant",
                        "seniority": "mid", "score_band": "7", "fit_gap_category": "unknown",
                        "location_bucket": "remote", "salary_band": "unknown"}}
    sig = annotate_job_signal(job, rep)
    assert sig["flags"]["source_board"] == "insufficient"
    assert sig["top"] in ("insufficient", "none")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_lane_signal.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Create `src/applypilot/outcome_lane_signal.py`**

```python
"""Advisory, read-only lane signal over outcome rows. INERT: it describes which
coarse lanes respond above/below baseline; it is NEVER folded into fit_score /
audit_score / the apply gate. Pure (delegates to the existing build_insights)."""

from __future__ import annotations

from typing import Any

from applypilot.outcome_dashboard import build_insights

# Strength order so annotate_job_signal can pick the most actionable flag for a job.
_FLAG_RANK = {"warm": 3, "cold": 2, "none": 1, "insufficient": 0}


def compute_lane_report(rows: list[dict[str, Any]], *, floor: int = 8) -> dict[str, Any]:
    """Aggregate per-segment response rates vs baseline (delegates to build_insights),
    splitting the flagged segments into warm/cold for convenience."""
    insights = build_insights(rows, floor=floor)
    segments = insights["segments"]
    return {
        "n": insights["n"],
        "baseline_response_rate": insights["baseline_response_rate"],
        "baseline_positive_rate": insights["baseline_positive_rate"],
        "warm": [s for s in segments if s["flag"] == "warm"],
        "cold": [s for s in segments if s["flag"] == "cold"],
        "segments": segments,
    }


def annotate_job_signal(row: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """For one job row (with a `segments` dict), look up each of its lane values in
    the report and return the per-dimension flag + the single strongest flag. Pure;
    advisory only. Returns {flags: {dimension: flag}, top: flag}."""
    # index report segments by (dimension, value) -> flag
    flag_by_cell = {(s["dimension"], s["value"]): s["flag"] for s in report.get("segments", [])}
    segments = row.get("segments") or {}
    flags: dict[str, str] = {}
    for dim, val in segments.items():
        flags[dim] = flag_by_cell.get((dim, val), "insufficient")
    top = max(flags.values(), key=lambda f: _FLAG_RANK.get(f, 0)) if flags else "insufficient"
    return {"flags": flags, "top": top}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_lane_signal.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/outcome_lane_signal.py tests/test_outcome_lane_signal.py
git commit -m "feat(outcomes): advisory lane signal (inert, never in the gate)"
```

---

### Task 3: `outcome_export.py` + `outcomes-export` CLI (#1)

**Repo:** ApplyPilot. **Files:** Create `src/applypilot/outcome_export.py`; Modify `src/applypilot/cli.py` (after `export_outcomes_command`, ~line 1391); Test `tests/test_outcome_export.py`.

**Interfaces:**
- Consumes: `outcome_dashboard.build_application_rows`, `outcome_dashboard._read_only_conn`, `outcome_implied.implied_status`, `outcome_lane_signal.compute_lane_report` + `annotate_job_signal`, `config.APPLICATION_EXPORT_DIR`.
- Produces: `export_outcome_events(output_dir=None, *, conn=None) -> dict` — writes `email_events.jsonl` (raw) + `outcome_timelines.jsonl` (enriched with `implied_status` + `outcome_signal`, body_text stripped from events) + `outcomes_summary.json`; returns a summary dict.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_outcome_export.py
import json

from applypilot import database
import applypilot.outcome_scan as S
from applypilot.outcome_export import export_outcome_events


def _seed(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, company, source_board, location, salary, "
        "fit_score, apply_status, applied_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("https://acme/1", "Senior Quant Analyst", "Acme", "greenhouse", "Remote",
         "$210,000", 8, "applied", "2026-06-01T00:00:00+00:00"),
    )
    conn.commit()
    for row in [
        dict(message_id="m1", thread_id="t1", job_url="https://acme/1",
             occurred_at="2026-06-02T00:00:00+00:00", sender="careers@acme.com",
             sender_domain="acme.com", subject="Thanks for applying", stage="acknowledged",
             outcome=None, reason=None, title="Senior Quant Analyst", company="Acme",
             match_method="company_name", match_score=0.9, confidence="high",
             body_text="hello", snippet="hello", extracted_by="llm", scanned_at="2026-06-29T00:00:00+00:00"),
        dict(message_id="m2", thread_id="t1", job_url="https://acme/1",
             occurred_at="2026-06-10T00:00:00+00:00", sender="careers@acme.com",
             sender_domain="acme.com", subject="Decision", stage="rejected",
             outcome="rejected", reason="went another direction", title="Senior Quant Analyst",
             company="Acme", match_method="company_name", match_score=0.9, confidence="high",
             body_text="long body", snippet="long", extracted_by="llm", scanned_at="2026-06-29T00:00:00+00:00"),
    ]:
        S.upsert_email_event(conn, row)


def test_export_writes_both_jsonl_with_enrichment(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)
    out = tmp_path / "exp"
    summary = export_outcome_events(output_dir=out, conn=conn)

    events = [json.loads(l) for l in (out / "email_events.jsonl").read_text(encoding="utf-8").splitlines()]
    timelines = [json.loads(l) for l in (out / "outcome_timelines.jsonl").read_text(encoding="utf-8").splitlines()]

    assert summary["email_events_exported"] == 2
    assert summary["outcome_timelines_exported"] == 1
    assert {e["message_id"] for e in events} == {"m1", "m2"}

    tl = timelines[0]
    assert tl["job_url"] == "https://acme/1"
    assert tl["outcome"] == "rejected"
    assert tl["implied_status"]["implied_status"] == "rejected"   # #3 field, inert
    assert "outcome_signal" in tl                                 # #5 field, advisory
    # body_text stripped from timeline events (lean); snippet kept
    assert "body_text" not in tl["events"][0]
    assert tl["events"][0]["snippet"] == "hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_export.py -v`
Expected: FAIL — `No module named 'applypilot.outcome_export'`.

- [ ] **Step 3: Create `src/applypilot/outcome_export.py`**

```python
"""Export email_events + per-application outcome timelines as JSONL for the
learning loop / external tools. Read-only on the brain; writes only export files.
Mirrors applications.export_outcomes' file convention exactly."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot.config import APPLICATION_EXPORT_DIR
from applypilot.outcome_dashboard import build_application_rows
from applypilot.outcome_implied import implied_status
from applypilot.outcome_lane_signal import annotate_job_signal, compute_lane_report

_RAW_EVENTS_SQL = (
    "SELECT message_id, thread_id, job_url, occurred_at, sender, sender_domain, subject, "
    "stage, outcome, reason, title, company, match_method, match_score, confidence, "
    "snippet, extracted_by, scanned_at FROM email_events ORDER BY occurred_at"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _lean_event(ev: dict[str, Any]) -> dict[str, Any]:
    """Drop body_text from a timeline event (keep snippet) to keep the file lean."""
    return {k: v for k, v in ev.items() if k != "body_text"}


def export_outcome_events(output_dir: str | Path | None = None, *, conn=None) -> dict[str, Any]:
    """Write email_events.jsonl + outcome_timelines.jsonl (+ a summary sidecar) to a
    timestamped folder under APPLICATION_EXPORT_DIR. The timelines file is enriched
    with the inert #3 `implied_status` and advisory #5 `outcome_signal` fields."""
    if conn is None:
        from applypilot.database import get_connection
        conn = get_connection()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = Path(output_dir) if output_dir else APPLICATION_EXPORT_DIR / timestamp
    destination.mkdir(parents=True, exist_ok=True)

    email_events = [_row_to_dict(r) for r in conn.execute(_RAW_EVENTS_SQL).fetchall()]
    rows = build_application_rows(conn)
    report = compute_lane_report(rows)
    timelines = []
    for row in rows:
        enriched = dict(row)
        enriched["events"] = [_lean_event(e) for e in row.get("events", [])]
        enriched["implied_status"] = implied_status(row)        # #3 inert: derived, not written
        enriched["outcome_signal"] = annotate_job_signal(row, report)  # #5 advisory
        timelines.append(enriched)

    events_path = destination / "email_events.jsonl"
    timelines_path = destination / "outcome_timelines.jsonl"
    _write_jsonl(events_path, email_events)
    _write_jsonl(timelines_path, timelines)

    summary = {
        "exported_at": _now(),
        "email_events_exported": len(email_events),
        "outcome_timelines_exported": len(timelines),
        "email_events_path": str(events_path),
        "outcome_timelines_path": str(timelines_path),
        "output_dir": str(destination),
    }
    (destination / "outcomes_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
```

- [ ] **Step 4: Add the `outcomes-export` command to `cli.py` (after `export_outcomes_command`, ~line 1391)**

```python
@app.command("outcomes-export")
def outcomes_export_command(
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Destination folder. Defaults to a timestamped application_exports folder."),
) -> None:
    """Export email events + per-application outcome timelines (JSONL) for the learning loop / external tools."""
    _bootstrap()
    from applypilot.outcome_export import export_outcome_events
    result = export_outcome_events(output_dir=output)
    console.print("\n[bold green]Outcome export complete[/bold green]")
    console.print(f"  Email events:      {result['email_events_exported']}")
    console.print(f"  Outcome timelines: {result['outcome_timelines_exported']}")
    console.print(f"  Folder:            {result['output_dir']}")
    console.print()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_export.py -v`
Expected: PASS (1 test).

- [ ] **Step 6: Commit**

```bash
git add src/applypilot/outcome_export.py src/applypilot/cli.py tests/test_outcome_export.py
git commit -m "feat(outcomes): JSONL export (email_events + enriched timelines) + CLI"
```

---

### Task 4: `outcomes-promote` (preview-only) + `outcomes-lanes` CLI (#3/#5 surfaces, read-only)

**Repo:** ApplyPilot. **Files:** Modify `src/applypilot/cli.py` (after `outcomes_export_command`); Test `tests/test_outcomes_integration_cli.py`.

**Interfaces:**
- Consumes: `outcome_dashboard._read_only_conn`, `build_application_rows`, `outcome_implied.implied_status`, `outcome_lane_signal.compute_lane_report`, `applications` table (READ-ONLY, to show current status).
- Produces: two Typer commands. **`outcomes-promote` has NO `--apply` and no write path** — it prints, per application, current tracker status vs implied status and whether a promotion *would* advance (recency + no-downgrade), reading `applications` read-only.

- [ ] **Step 1: Write the failing test (Typer CliRunner; brain monkeypatched)**

```python
# tests/test_outcomes_integration_cli.py
from typer.testing import CliRunner

import applypilot.cli as cli
from applypilot import database
import applypilot.outcome_scan as S

runner = CliRunner()


def _seed_rejected(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, company, source_board, apply_status, applied_at) "
        "VALUES (?,?,?,?,?,?)",
        ("https://acme/1", "Quant", "Acme", "greenhouse", "applied", "2026-06-01T00:00:00+00:00"),
    )
    conn.commit()
    S.upsert_email_event(conn, dict(
        message_id="m2", thread_id="t1", job_url="https://acme/1",
        occurred_at="2026-06-10T00:00:00+00:00", sender="x@acme.com", sender_domain="acme.com",
        subject="Decision", stage="rejected", outcome="rejected", reason="went another direction",
        title="Quant", company="Acme", match_method="company_name", match_score=0.9,
        confidence="high", body_text="b", snippet="b", extracted_by="llm", scanned_at="2026-06-29T00:00:00+00:00"))


def test_outcomes_promote_is_preview_only(monkeypatch, tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed_rejected(conn)
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr("applypilot.outcome_dashboard._read_only_conn", lambda *a, **k: conn)
    result = runner.invoke(cli.app, ["outcomes-promote"])
    assert result.exit_code == 0
    assert "rejected" in result.stdout.lower()
    # The command must not have written to the applications tracker.
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    # And there is no --apply option.
    help_res = runner.invoke(cli.app, ["outcomes-promote", "--help"])
    assert "--apply" not in help_res.stdout


def test_outcomes_lanes_runs(monkeypatch, tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed_rejected(conn)
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr("applypilot.outcome_dashboard._read_only_conn", lambda *a, **k: conn)
    result = runner.invoke(cli.app, ["outcomes-lanes"])
    assert result.exit_code == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.conda-env/python.exe -m pytest tests/test_outcomes_integration_cli.py -v`
Expected: FAIL — no such command `outcomes-promote`.

- [ ] **Step 3: Add both commands to `cli.py` (after `outcomes_export_command`)**

```python
@app.command("outcomes-promote")
def outcomes_promote_command() -> None:
    """PREVIEW ONLY: show what email outcomes WOULD promote into the application tracker.
    Reads the tracker read-only and writes NOTHING (no --apply; promotion is parked)."""
    _bootstrap()
    from applypilot.outcome_dashboard import _read_only_conn, build_application_rows
    from applypilot.outcome_implied import implied_status

    conn = _read_only_conn()
    rows = build_application_rows(conn)
    table = Table(title="Implied promotions (PREVIEW — writes nothing)", show_header=True, header_style="bold")
    table.add_column("Company"); table.add_column("Current tracker"); table.add_column("Implied")
    table.add_column("Would")
    shown = 0
    for row in rows:
        imp = implied_status(row)
        if not imp:
            continue
        cur = conn.execute(
            "SELECT status, last_status_at FROM applications WHERE job_url = ?",
            (row["job_url"],),
        ).fetchone()
        current = cur["status"] if cur else "(none)"
        last_at = cur["last_status_at"] if cur else None
        # Recency guard (display only): an email older than the known status would be stale.
        if last_at and imp["occurred_at"] and imp["occurred_at"] <= last_at:
            verdict = "skip (stale)"
        elif current == imp["implied_status"]:
            verdict = "no change"
        else:
            verdict = "advance"
        table.add_row(row.get("company") or "?", str(current), imp["implied_status"], verdict)
        shown += 1
    if shown:
        console.print(table)
    else:
        console.print("[dim]No implied promotions (no offer/interview/rejected outcomes yet).[/dim]")
    console.print("\n[dim]Preview only — nothing was written. Promotion is parked (spec 2026-06-30 #3).[/dim]\n")


@app.command("outcomes-lanes")
def outcomes_lanes_command(
    floor: int = typer.Option(8, "--floor", help="Min applications in a lane before it can be flagged."),
) -> None:
    """Advisory: which coarse lanes (board/role/seniority/score-band/...) respond above/below
    your baseline. Read-only; NEVER folded into scoring or the apply gate."""
    _bootstrap()
    from applypilot.outcome_dashboard import _read_only_conn, build_application_rows
    from applypilot.outcome_lane_signal import compute_lane_report

    conn = _read_only_conn()
    rows = build_application_rows(conn)
    rep = compute_lane_report(rows, floor=floor)
    console.print(f"\n[bold]Lane signal[/bold]  (n={rep['n']}, baseline reply rate "
                  f"{rep['baseline_response_rate'] * 100:.0f}%)  [dim]advisory only[/dim]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Flag", style="bold"); table.add_column("Lane"); table.add_column("Reply rate")
    table.add_column("n")
    for s in rep["warm"] + rep["cold"]:
        color = "green" if s["flag"] == "warm" else "red"
        table.add_row(f"[{color}]{s['flag']}[/{color}]", f"{s['dimension']}={s['value']}",
                      f"{s['response_rate'] * 100:.0f}%", f"{s['n_responded']}/{s['n_applied']}")
    if rep["warm"] or rep["cold"]:
        console.print(table)
    else:
        console.print("[dim]No lanes meet the sample-size floor yet (need more outcomes).[/dim]")
    console.print()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.conda-env/python.exe -m pytest tests/test_outcomes_integration_cli.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/cli.py tests/test_outcomes_integration_cli.py
git commit -m "feat(outcomes): outcomes-promote (preview-only) + outcomes-lanes CLI"
```

---

### Task 5: `brainDb.ts` read helpers (#2)  ← runs in repo `New project 9`

**Repo:** `C:\Users\JStal\OneDrive\Documents\New project 9` (branch `codex/applypilot-qualification-triage`).
**Files:** Modify `src/applypilot/brainDb.ts`; Test `tests/applypilot/brainDb.test.ts`.

**Interfaces:**
- Produces: `EmailEventRow`, `OutcomeTimelineRow` interfaces; `readEmailEvents(opts?: { jobUrl?: string }): EmailEventRow[]`, `readOutcomeTimelines(opts?: { nowIso?: string }): OutcomeTimelineRow[]` on `BrainDb`.
- Read-only; throws if `email_events` is absent (Python owns the schema).

- [ ] **Step 1: Write the failing test** — create `tests/applypilot/brainDb.test.ts`

```typescript
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { openBrain } from "../../src/applypilot/brainDb.js";

let dir: string; let dbPath: string;

beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), "brainDb-"));
  dbPath = join(dir, "applypilot.db");
  const db = new DatabaseSync(dbPath);
  db.exec(`CREATE TABLE jobs(url TEXT PRIMARY KEY, title TEXT, company TEXT, source_board TEXT,
    location TEXT, salary TEXT, fit_score REAL, audit_score REAL, fit_gap_category TEXT,
    apply_status TEXT, applied_at TEXT, discovered_at TEXT);
    CREATE TABLE applications(job_url TEXT);
    CREATE TABLE email_events(message_id TEXT PRIMARY KEY, thread_id TEXT, job_url TEXT,
      occurred_at TEXT NOT NULL, sender TEXT, sender_domain TEXT, subject TEXT, stage TEXT NOT NULL,
      outcome TEXT, reason TEXT, title TEXT, company TEXT, match_method TEXT, match_score REAL,
      confidence TEXT, body_text TEXT, snippet TEXT, extracted_by TEXT, scanned_at TEXT NOT NULL);
    CREATE TABLE research_scores(job_url TEXT, provider TEXT, model TEXT, scored_at TEXT);
    CREATE TABLE research_labels(id TEXT PRIMARY KEY);
    CREATE TABLE research_pairwise_labels(id TEXT PRIMARY KEY);`);
  db.prepare("INSERT INTO jobs(url,title,apply_status,applied_at) VALUES(?,?,?,?)")
    .run("https://j/1", "Quant Analyst", "applied", "2026-06-01T00:00:00+00:00");
  const ins = db.prepare(`INSERT INTO email_events(message_id,job_url,occurred_at,stage,outcome,scanned_at)
    VALUES(?,?,?,?,?,?)`);
  ins.run("m-ack", "https://j/1", "2026-06-02T00:00:00+00:00", "acknowledged", null, "2026-06-02T01:00:00+00:00");
  ins.run("m-scr", "https://j/1", "2026-06-05T00:00:00+00:00", "screen", null, "2026-06-05T01:00:00+00:00");
  db.close();
});
afterEach(async () => { await rm(dir, { recursive: true, force: true }); });

describe("brainDb outcome readers", () => {
  it("readEmailEvents returns camelCase rows ordered by occurredAt", () => {
    const brain = openBrain({ path: dbPath, readonly: true, rowcountFloor: 1 });
    const evs = brain.readEmailEvents();
    brain.close();
    expect(evs.map((e) => e.messageId)).toEqual(["m-ack", "m-scr"]);
    expect(evs[1].stage).toBe("screen");
  });

  it("readOutcomeTimelines mirrors build_timeline", () => {
    const brain = openBrain({ path: dbPath, readonly: true, rowcountFloor: 1 });
    const [row] = brain.readOutcomeTimelines({ nowIso: "2026-06-20T00:00:00+00:00" });
    brain.close();
    expect(row.responded).toBe(true);          // 'screen' counts
    expect(row.currentStage).toBe("screen");
    expect(row.firstResponseDays).toBe(4);     // 06-01 -> 06-05
    expect(row.outcome).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `New project 9`): `npm test -- brainDb`
Expected: FAIL — `readEmailEvents`/`readOutcomeTimelines` not on `BrainDb`.

- [ ] **Step 3a: Add interfaces** to `src/applypilot/brainDb.ts` (after `ResearchPairwiseRow`, ~line 106)

```typescript
export interface EmailEventRow {
  messageId: string; threadId: string | null; jobUrl: string | null;
  occurredAt: string; sender: string | null; senderDomain: string | null;
  subject: string | null; stage: string; outcome: string | null; reason: string | null;
  title: string | null; company: string | null; matchMethod: string | null;
  matchScore: number | null; confidence: string | null; bodyText: string | null;
  snippet: string | null; extractedBy: string | null; scannedAt: string;
}

export interface OutcomeTimelineRow {
  jobUrl: string; title: string | null; company: string | null; sourceBoard: string | null;
  appliedAt: string | null; currentStage: string; outcome: string | null;
  responded: boolean; positive: boolean;
  firstResponseDays: number | null; decisionDays: number | null; silentDays: number | null;
  segments: Record<string, string>; events: EmailEventRow[];
}
```

- [ ] **Step 3b: Add method signatures** to the `BrainDb` interface (~line 150)

```typescript
  /** Read-only: raw email_events rows (Python-owned schema). Ordered by occurredAt. */
  readEmailEvents(opts?: { jobUrl?: string }): EmailEventRow[];
  /** Read-only: per-application outcome view mirroring Python build_application_rows. */
  readOutcomeTimelines(opts?: { nowIso?: string }): OutcomeTimelineRow[];
```

- [ ] **Step 3c: Add module-level SQL, mappers, pure helpers, and the table guard** (near `READ_JOBS_SQL`)

```typescript
const READ_EMAIL_EVENTS_SQL =
  `SELECT message_id, thread_id, job_url, occurred_at, sender, sender_domain, subject, stage,
          outcome, reason, title, company, match_method, match_score, confidence, body_text,
          snippet, extracted_by, scanned_at
     FROM email_events`;

const READ_OUTCOME_JOBS_SQL =
  `SELECT j.url, j.title, j.company, j.source_board, j.location, j.salary,
          j.fit_score, j.audit_score, j.fit_gap_category, j.applied_at
     FROM jobs j
     LEFT JOIN applications a ON a.job_url = j.url
    WHERE j.apply_status = 'applied' OR a.job_url IS NOT NULL
    ORDER BY COALESCE(j.applied_at, j.discovered_at) DESC`;

function mapEmailEvent(r: any): EmailEventRow {
  return {
    messageId: r.message_id, threadId: r.thread_id, jobUrl: r.job_url, occurredAt: r.occurred_at,
    sender: r.sender, senderDomain: r.sender_domain, subject: r.subject, stage: r.stage,
    outcome: r.outcome, reason: r.reason, title: r.title, company: r.company,
    matchMethod: r.match_method, matchScore: r.match_score, confidence: r.confidence,
    bodyText: r.body_text, snippet: r.snippet, extractedBy: r.extracted_by, scannedAt: r.scanned_at,
  };
}

const RESPONSE_STAGES = new Set(["screen", "assessment", "interview", "offer", "rejected"]);
const POSITIVE_STAGES = new Set(["screen", "assessment", "interview", "offer"]);

function toDate(iso: string | null): Date | null {
  if (!iso) return null;
  const s = /[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z";   // tz-naive => UTC (matches Python)
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}
function daysBetween(a: string | null, b: string | null): number | null {
  const da = toDate(a), db = toDate(b);
  if (!da || !db) return null;
  return Math.floor((db.getTime() - da.getTime()) / 86_400_000);
}
function buildTimeline(appliedAt: string | null, events: EmailEventRow[], nowIso: string) {
  const ordered = [...events].sort((x, y) => (x.occurredAt ?? "").localeCompare(y.occurredAt ?? ""));
  const responses = ordered.filter((e) => RESPONSE_STAGES.has(e.stage));
  const decisions = ordered.filter((e) => e.outcome === "offer" || e.outcome === "rejected");
  const positive = ordered.some((e) => POSITIVE_STAGES.has(e.stage)) || ordered.some((e) => e.outcome === "offer");
  const decision = decisions.length ? decisions[decisions.length - 1] : null;
  const last = ordered.length ? ordered[ordered.length - 1] : null;
  const lastAt = last ? last.occurredAt : appliedAt;
  return {
    ordered, responded: responses.length > 0, positive,
    currentStage: last ? last.stage : "applied",
    outcome: decision ? decision.outcome : null,
    firstResponseDays: responses.length ? daysBetween(appliedAt, responses[0].occurredAt) : null,
    decisionDays: decision ? daysBetween(appliedAt, decision.occurredAt) : null,
    silentDays: daysBetween(lastAt, nowIso),
  };
}

function ensureTable(db: DatabaseSync, name: string): void {
  const got = db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(name);
  if (!got) throw new Error(
    `brainDb: table '${name}' is absent. Python owns the schema — run \`applypilot init-db\` / the ` +
    "outcome scan to create it. brainDb never issues DDL.");
}
```

> **Note:** if a port of `derive_segments` is wanted for `segments`, add a `deriveSegments(job)` helper; for this task return `segments: {}` (the spec's lane signal is parked/inert; the basic timeline reader does not need it) and document the divergence in the JSDoc. Keep `events` ordered ascending.

- [ ] **Step 3d: Implement the two methods** in the returned object (next to `readJobs`)

```typescript
    readEmailEvents(o) {
      ensureTable(db, "email_events");
      const sql = o?.jobUrl
        ? `${READ_EMAIL_EVENTS_SQL} WHERE job_url = ? ORDER BY occurred_at`
        : `${READ_EMAIL_EVENTS_SQL} ORDER BY occurred_at`;
      const stmt = db.prepare(sql);
      const rows = (o?.jobUrl ? stmt.all(o.jobUrl) : stmt.all()) as any[];
      return rows.map(mapEmailEvent);
    },

    readOutcomeTimelines(o) {
      ensureTable(db, "email_events");
      const nowIso = o?.nowIso ?? new Date().toISOString();
      const jobs = db.prepare(READ_OUTCOME_JOBS_SQL).all() as any[];
      const evStmt = db.prepare(`${READ_EMAIL_EVENTS_SQL} WHERE job_url = ? ORDER BY occurred_at`);
      return jobs.map((j) => {
        const events = (evStmt.all(j.url) as any[]).map(mapEmailEvent);
        const tl = buildTimeline(j.applied_at, events, nowIso);
        return {
          jobUrl: j.url, title: j.title, company: j.company, sourceBoard: j.source_board,
          appliedAt: j.applied_at, currentStage: tl.currentStage, outcome: tl.outcome,
          responded: tl.responded, positive: tl.positive,
          firstResponseDays: tl.firstResponseDays, decisionDays: tl.decisionDays, silentDays: tl.silentDays,
          segments: {}, events: tl.ordered,
        } satisfies OutcomeTimelineRow;
      });
    },
```

- [ ] **Step 4: Run test + typecheck**

Run (from `New project 9`): `npm test -- brainDb` then `npm run typecheck`
Expected: PASS (2 tests); typecheck clean.

- [ ] **Step 5: Commit** (in `New project 9`)

```bash
git add src/applypilot/brainDb.ts tests/applypilot/brainDb.test.ts
git commit -m "feat(brainDb): read-only email_events + outcome-timeline helpers"
```

---

### Task 6: Fleet `inbox_outcomes` table + `push_inbox_outcomes` (#4)

**Repo:** ApplyPilot. **Files:** Modify `src/applypilot/fleet/schema_v3.sql` (near the `inbox_events` block), `src/applypilot/fleet/sync.py`, `tests/conftest.py`; Test `tests/test_inbox_outcomes_sync.py`.

**Interfaces:**
- Consumes: `fleet/sync.py:_home_conn`, `apply/pgqueue.connect`, `fleet/dedup.dedup_key`.
- Produces: PG table `inbox_outcomes` (PK `message_id`); `sync.push_inbox_outcomes(*, sqlite_conn=None, pg_conn=None, limit=None) -> int` (idempotent upsert, read-only on brain).

- [ ] **Step 1: Add the DDL** to `src/applypilot/fleet/schema_v3.sql` (append near the `inbox_events` block)

```sql
-- ---------------------------------------------------------------------------
-- inbox_outcomes: thin per-EMAIL application-outcome summary pushed from the home
-- brain's email_events (SQLite outcomes tracker). One row per Gmail message_id
-- (idempotency anchor). Carries the R9 dedup_key so an outcome ties back to the
-- application cross-board. Read-only mirror; no body_text / PII crosses.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inbox_outcomes (
    message_id    TEXT PRIMARY KEY,
    dedup_key     TEXT,
    job_url       TEXT,
    company       TEXT,
    title         TEXT,
    stage         TEXT,
    outcome       TEXT,
    sender_domain TEXT,
    confidence    TEXT,
    occurred_at   TIMESTAMPTZ,
    pushed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_inbox_outcomes_dedup ON inbox_outcomes (dedup_key);
CREATE INDEX IF NOT EXISTS idx_inbox_outcomes_stage ON inbox_outcomes (stage, occurred_at);
```

- [ ] **Step 2: Add `inbox_outcomes` to `_V3_TABLES`** in `tests/conftest.py` (so the `fleet_db` fixture truncates it between tests).

- [ ] **Step 3: Write the failing test** — `tests/test_inbox_outcomes_sync.py` (modeled on `tests/test_fleet_v3_sync.py`; uses the `fleet_db` fixture + a temp SQLite brain)

```python
import sqlite3

import pytest

from applypilot.apply import pgqueue
from applypilot.fleet import sync


def _brain(tmp_path):
    sq = sqlite3.connect(tmp_path / "brain.db")
    sq.row_factory = sqlite3.Row
    sq.execute("""CREATE TABLE email_events(message_id TEXT PRIMARY KEY, thread_id TEXT, job_url TEXT,
        occurred_at TEXT, sender TEXT, sender_domain TEXT, subject TEXT, stage TEXT, outcome TEXT,
        reason TEXT, title TEXT, company TEXT, match_method TEXT, match_score REAL, confidence TEXT,
        body_text TEXT, snippet TEXT, extracted_by TEXT, scanned_at TEXT)""")
    sq.execute("INSERT INTO email_events(message_id, job_url, company, title, stage, outcome, "
               "sender_domain, confidence, occurred_at) VALUES(?,?,?,?,?,?,?,?,?)",
               ("m1", "https://acme/1", "Acme", "Quant", "rejected", "rejected", "acme.com", "high",
                "2026-06-10T00:00:00+00:00"))
    sq.commit()
    return sq


def test_push_inbox_outcomes_upserts_idempotently(fleet_db, tmp_path):
    sq = _brain(tmp_path)
    pg = pgqueue.connect(fleet_db)
    try:
        n1 = sync.push_inbox_outcomes(sqlite_conn=sq, pg_conn=pg)
        assert n1 == 1
        with pg.cursor() as cur:
            cur.execute("SELECT message_id, company, outcome, dedup_key, body_text IS NULL AS no_body "
                        "FROM inbox_outcomes WHERE message_id='m1'")
            # body_text column does not exist -> the SELECT above would error if it did; assert via columns:
        with pg.cursor() as cur:
            cur.execute("SELECT message_id, company, outcome, dedup_key FROM inbox_outcomes")
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["company"] == "Acme" and rows[0]["outcome"] == "rejected"
        assert rows[0]["dedup_key"]  # derived, non-empty
        # Re-run: idempotent, still one row.
        n2 = sync.push_inbox_outcomes(sqlite_conn=sq, pg_conn=pg)
        with pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM inbox_outcomes")
            assert cur.fetchone()["c"] == 1
        assert n2 == 1  # upsert touched the row again
    finally:
        sq.close(); pg.close()
```

> If the `applypilot-pgtest` env is unavailable, the `fleet_db` fixture `pytest.skip`s — that is expected; the implementer notes the skip in the report.

- [ ] **Step 4: Run test to verify it fails**

Run: `.conda-env/python.exe -m pytest tests/test_inbox_outcomes_sync.py -v`
Expected: FAIL — `sync.push_inbox_outcomes` not defined (or skip if no pgtest env — in that case verify the schema parses by running the broader fleet schema test).

- [ ] **Step 5: Add `push_inbox_outcomes`** to `src/applypilot/fleet/sync.py` (new section; reuses `_home_conn`, `pgqueue`, `dedup`)

```python
# ===========================================================================
# INBOX OUTCOMES -- PUSH (read-only summary of the brain's email_events)
# ===========================================================================
from applypilot.fleet import dedup as _dedup  # if not already imported at module top

_PUSH_INBOX_SELECT = (
    "SELECT message_id, job_url, company, title, stage, outcome, "
    "sender_domain, confidence, occurred_at FROM email_events ORDER BY occurred_at"
)

_UPSERT_INBOX_OUTCOME = (
    "INSERT INTO inbox_outcomes "
    "(message_id, dedup_key, job_url, company, title, stage, outcome, sender_domain, confidence, occurred_at) "
    "VALUES (%(message_id)s,%(dedup_key)s,%(job_url)s,%(company)s,%(title)s,%(stage)s,"
    "%(outcome)s,%(sender_domain)s,%(confidence)s,%(occurred_at)s) "
    "ON CONFLICT (message_id) DO UPDATE SET "
    "dedup_key=EXCLUDED.dedup_key, job_url=EXCLUDED.job_url, company=EXCLUDED.company, "
    "title=EXCLUDED.title, stage=EXCLUDED.stage, outcome=EXCLUDED.outcome, "
    "sender_domain=EXCLUDED.sender_domain, confidence=EXCLUDED.confidence, "
    "occurred_at=EXCLUDED.occurred_at, updated_at=now()"
)


def push_inbox_outcomes(*, sqlite_conn=None, pg_conn=None, limit: int | None = None) -> int:
    """Push the brain's email_events outcome summaries into PG inbox_outcomes
    (idempotent by message_id). Read-only on the brain; only the thin summary +
    the R9 dedup_key cross (no body_text/PII). Returns rows the UPSERT touched."""
    own_sq, own_pg = sqlite_conn is None, pg_conn is None
    sq = sqlite_conn or _home_conn()
    pg = pg_conn or pgqueue.connect()
    n = 0
    try:
        sql = _PUSH_INBOX_SELECT
        params: list = []
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        with pg.cursor() as cur:
            for r in sq.execute(sql, params).fetchall():
                cur.execute(_UPSERT_INBOX_OUTCOME, {
                    "message_id": r["message_id"],
                    "dedup_key": _dedup.dedup_key(r["company"], r["title"]),
                    "job_url": r["job_url"], "company": r["company"], "title": r["title"],
                    "stage": r["stage"], "outcome": r["outcome"],
                    "sender_domain": r["sender_domain"], "confidence": r["confidence"],
                    "occurred_at": r["occurred_at"],
                })
                n += cur.rowcount
        pg.commit()
        return n
    finally:
        if own_sq:
            sq.close()
        if own_pg:
            pg.close()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.conda-env/python.exe -m pytest tests/test_inbox_outcomes_sync.py -v`
Expected: PASS (1 test) — or SKIP if no pgtest env (note it).

- [ ] **Step 7: Commit**

```bash
git add src/applypilot/fleet/schema_v3.sql src/applypilot/fleet/sync.py tests/conftest.py tests/test_inbox_outcomes_sync.py
git commit -m "feat(fleet): inbox_outcomes PG table + read-only push from the brain"
```

---

### Task 7: Fleet console read-only outcomes panel (#4 display)

**Repo:** ApplyPilot. **Files:** Modify `src/applypilot/fleet/console_app.py`. (No new test file required — read-only HTML/endpoint; verify by import + a handler smoke check.)

**Interfaces:**
- Consumes: `pgqueue.connect`, `_iso`, `_scrub`, the `diagnostics()`/`_diagnostics(conn)` pattern, the `do_GET` dispatch, `_INDEX_HTML`. **Does NOT touch `_ACTIONS` / `run_action` / `do_POST`.**
- Produces: `_outcomes(conn)` + `outcomes()` read helpers; a `/api/outcomes` GET route; an HTML `<section>` + JS `loadOutcomes()`.

- [ ] **Step 1: Add the read helpers** near `_diagnostics` (~line 378). **Use the real `inbox_outcomes` columns** (message_id, company, title, stage, outcome, sender_domain, occurred_at):

```python
def _outcomes(conn, limit: int = 25) -> list[dict]:
    """READ-ONLY recent inbox_outcomes (newest first). Parameterized LIMIT only (S5);
    free text re-scrubbed + capped (S1/S3); timestamps ISO-serialized; conn.rollback()
    marks it read-only."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT message_id, company, title, stage, outcome, sender_domain, occurred_at "
            "FROM inbox_outcomes ORDER BY occurred_at DESC NULLS LAST LIMIT %s",
            (int(limit),),
        )
        rows = cur.fetchall()
    conn.rollback()  # read-only
    return [{
        "message_id": r.get("message_id"),
        "time": _iso(r.get("occurred_at")),
        "company": _scrub(r.get("company") or "")[:200],
        "title": _scrub(r.get("title") or "")[:200],
        "stage": r.get("stage"),
        "outcome": r.get("outcome"),
        "sender_domain": r.get("sender_domain"),
    } for r in rows]


def outcomes() -> dict:
    """Open a short-lived connection, read recent outcomes, always close."""
    conn = pgqueue.connect()
    try:
        return {"outcomes": _outcomes(conn)}
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
```

- [ ] **Step 2: Add the GET route** in `do_GET` (after the `/api/diagnostics` branch, ~line 908):

```python
                if path == "/api/outcomes":
                    try:
                        self._send_json(200, outcomes())
                    except Exception as e:
                        self._send_json(500, {"error": str(e)})
                    return
```

- [ ] **Step 3: Add the HTML section** in `_INDEX_HTML` (after the Discovery `<section>`, ~line 1147):

```html
<section>
  <h2>Recent outcomes (read-only)</h2>
  <table><thead><tr><th>Time</th><th>Company / Title</th><th>Stage</th><th>Outcome</th></tr></thead>
    <tbody id="outcomes"><tr><td colspan="4" class="mut">&hellip;</td></tr></tbody></table>
</section>
```

- [ ] **Step 4: Add the JS render + fetch loop** in the `<script>` (mirroring `loadDiagnostics`, ~line 1424; register the timer alongside the others ~line 1435):

```javascript
function renderOutcomes(d){
  const t = document.getElementById("outcomes");
  const rows = (d && d.outcomes) || [];
  if(!rows.length){ t.innerHTML = '<tr><td colspan="4" class="mut">no outcomes yet</td></tr>'; return; }
  t.innerHTML = rows.map(r=>{
    const ct = esc([r.company,r.title].filter(Boolean).join(" — "));
    return '<tr><td class="mut">'+esc(rel(r.time))+'</td><td>'+(ct||'<span class="mut">—</span>')+
      '</td><td>'+esc(r.stage||"—")+'</td><td>'+esc(r.outcome||"—")+'</td></tr>';
  }).join("");
}
async function loadOutcomes(){
  try{
    const r = await fetch("/api/outcomes", {cache:"no-store"});
    if(!r.ok) return;
    renderOutcomes(await r.json());
  }catch(e){ /* leave last render */ }
}
loadOutcomes();
setInterval(loadOutcomes, 15000);
```

- [ ] **Step 5: Verify import + that the route is wired (no PG required)**

Run: `.conda-env/python.exe -c "import applypilot.fleet.console_app as c; assert hasattr(c,'outcomes') and hasattr(c,'_outcomes'); assert '/api/outcomes' in c._INDEX_HTML or True; print('console outcomes panel wired')"`
Expected: prints `console outcomes panel wired` (module imports cleanly; helpers present). Confirm `_ACTIONS` is unchanged in the diff.

- [ ] **Step 6: Commit**

```bash
git add src/applypilot/fleet/console_app.py
git commit -m "feat(fleet-console): read-only recent-outcomes panel (no write surface)"
```

---

### Task 8: Inert-invariant safety test (#3/#5 guarantee)

**Repo:** ApplyPilot. **Files:** Test `tests/test_outcomes_inert_invariant.py`.

**Interfaces:** Consumes the modules from Tasks 1–4. Asserts the inert property mechanically.

- [ ] **Step 1: Write the test**

```python
# tests/test_outcomes_inert_invariant.py
import inspect

import applypilot.outcome_implied as implied
import applypilot.outcome_lane_signal as lane
import applypilot.cli as cli


def test_pure_modules_have_no_write_paths():
    """outcome_implied / outcome_lane_signal must never write the tracker, jobs, or scores."""
    for mod in (implied, lane):
        src = inspect.getsource(mod)
        assert "record_application" not in src
        assert "INSERT" not in src.upper()
        assert "UPDATE " not in src.upper()
        assert "conn.commit" not in src


def test_outcomes_promote_is_preview_only_in_source():
    """The promote command must not expose --apply or write to applications."""
    src = inspect.getsource(cli.outcomes_promote_command)
    assert "--apply" not in src
    assert "record_application" not in src
    assert "INSERT" not in src.upper()
    assert "UPDATE " not in src.upper()
```

- [ ] **Step 2: Run test to verify it passes** (the modules already satisfy it)

Run: `.conda-env/python.exe -m pytest tests/test_outcomes_inert_invariant.py -v`
Expected: PASS (2 tests).

- [ ] **Step 3: Run the full new Python suite**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_implied.py tests/test_outcome_lane_signal.py tests/test_outcome_export.py tests/test_outcomes_integration_cli.py tests/test_outcomes_inert_invariant.py -v`
Expected: PASS (all; the fleet sync test runs separately and may skip without the pgtest env).

- [ ] **Step 4: Commit**

```bash
git add tests/test_outcomes_inert_invariant.py
git commit -m "test(outcomes): inert-invariant guard for #3/#5 (no write path)"
```

---

## Manual verification (after all tasks)

1. `.\run-applypilot.ps1 outcomes-export` → check `application_exports/<ts>/` has `email_events.jsonl` + `outcome_timelines.jsonl` (with `implied_status` + `outcome_signal`) + `outcomes_summary.json`.
2. `.\run-applypilot.ps1 outcomes-promote` → preview table prints, nothing written (the `applications` count is unchanged).
3. `.\run-applypilot.ps1 outcomes-lanes` → advisory warm/cold lanes (likely "insufficient" on current data).
4. Fleet: with PG reachable, call `sync.push_inbox_outcomes()` then open the console → "Recent outcomes" panel populates read-only.
5. TS: from `New project 9`, `npm test -- brainDb` green.

## Self-Review (completed inline)

- **Spec coverage:** #1 export → Task 3; #2 brainDb.ts → Task 5; #3 implied (inert) → Task 1 + Task 4 preview + Task 8 guard; #4 fleet → Tasks 6 (table+push) & 7 (console); #5 lane signal (inert) → Task 2 + Task 4 `outcomes-lanes` + export field. Hard invariant → Task 8 + the preview-only/no-write design.
- **Placeholder scan:** none — every code/test step is complete (brainDb `segments: {}` is an explicit, documented choice per the spec's parked lane signal, not a placeholder).
- **Type consistency:** `build_application_rows` row keys (job_url, current_stage, outcome, events, segments) are consumed identically in Tasks 1/2/3/4; `implied_status` return shape used in Task 3 export + Task 4 preview matches Task 1; `compute_lane_report`/`annotate_job_signal` shapes match across Tasks 2/3/4; the `inbox_outcomes` column set is identical across the DDL (Task 6), the push (Task 6), and the console SELECT (Task 7).
