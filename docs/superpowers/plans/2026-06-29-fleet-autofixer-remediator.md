# Fleet Auto-Fixer (Remediator) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A standalone, deterministic remediator that autonomously re-queues usage-limit-casualty jobs (provably never submitted) behind a 3-layer double-apply guard, to clear the dominant `no_result_line` failure class without manual keep-up.

**Architecture:** New `src/applypilot/fleet/remediator.py` (home box). It reads usage-limit diagnoses (`fleet_diagnoses`) + ground-truth signals (`applied_set`, brain `email_events`), and performs ONE expansionary action — flip a parked job back to `queued` — only when all three guards pass. Bounded per-pass + per-job, every action audited + reversible. The Doctor stays conservative-pure; the diagnoser stays advisory-pure.

**Tech Stack:** Python 3.11, psycopg (fleet PG, `dict_row` cursors via `applypilot.apply.pgqueue.connect`), sqlite3 (brain, read-only), pytest. Spec: `docs/superpowers/specs/2026-06-29-fleet-autofixer-design.md`.

## Global Constraints

- **NEVER double-apply.** A job is re-queued only if ALL 3 guards pass: (1) diagnosis proves never-submitted (`fleet_diagnoses.reason='usage_limit'`), (2) `dedup_key` NOT in `applied_set`, (3) NO confirming `email_events` row for the url. Any doubt → recommend, never re-queue.
- **ATS lane only.** `lane='ats'`; LinkedIn-lane jobs are never candidates and never re-queued.
- **Deterministic, no LLM** in the action path. `$0/pass`.
- **Bounded:** per-pass cap `max_requeue` (default 50); per-job cap `max_per_job` (default 2) → then leave parked + recommend.
- **Reversible + audited:** every re-queue writes a `remediation_actions` row with prior `(status, attempts, apply_error)` and `how_to_reverse`.
- **Graceful degradation:** missing `email_events` (table absent/empty) → guard 3 skipped (never a crash, never weaker than guards 1–2). Missing diagnoses → fewer candidates.
- **Re-queue reverses the reclaim park exactly:** reclaim parks as `status='crash_unconfirmed', apply_error='crash_unconfirmed', attempts=99, lease cleared` (`pgqueue.py`); re-queue sets `status='queued', attempts=0, lease cleared, apply_error='requeued_by_remediator:usage_limit'`.
- **Conda python (tests):** `C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot\.conda-env\python.exe` (env ROOT, not `Scripts\`). `git add` only the named files (never `-A`).
- Cursors are `dict_row`: read columns by NAME. Brain path: `from applypilot.config import DB_PATH`.

---

### Task 1: Dataclasses + `ensure_remediation_table`

**Files:**
- Create: `src/applypilot/fleet/remediator.py`
- Test: `tests/test_remediator.py`

**Interfaces:**
- Produces: `Candidate(url, worker_id, dedup_key, status, attempts, apply_error, reason)` dataclass; `ensure_remediation_table(conn) -> None` (idempotent `CREATE TABLE IF NOT EXISTS` + index, commits).

- [ ] **Step 1: Write the failing test**

Create `tests/test_remediator.py`:
```python
from applypilot.fleet import remediator


class _FakeCursor:
    def __init__(self, script):
        self.script = script; self.executed = []; self._last = None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        for needle, rows in self.script.items():
            if needle in sql: self._last = list(rows); return
        self._last = []
    def fetchone(self): return self._last[0] if self._last else None
    def fetchall(self): return self._last


class _FakeConn:
    def __init__(self, script=None):
        self._cur = _FakeCursor(script or {}); self.committed = False
    def cursor(self): return self._cur
    def commit(self): self.committed = True


def test_candidate_dataclass_fields():
    c = remediator.Candidate(url="u", worker_id="m2-3", dedup_key="dk",
                             status="crash_unconfirmed", attempts=99,
                             apply_error="crash_unconfirmed", reason="usage_limit")
    assert c.url == "u" and c.worker_id == "m2-3" and c.attempts == 99


def test_ensure_remediation_table_creates_idempotently():
    conn = _FakeConn()
    remediator.ensure_remediation_table(conn)
    sql = " ".join(s for s, _ in conn._cur.executed)
    assert "CREATE TABLE IF NOT EXISTS remediation_actions" in sql
    assert conn.committed is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `.conda-env\python.exe -m pytest tests/test_remediator.py -v`
Expected: FAIL — `module 'applypilot.fleet.remediator' has no attribute ...`.

- [ ] **Step 3: Implement**

Create `src/applypilot/fleet/remediator.py`:
```python
"""Fleet remediator (Phase 2 auto-fixer). Deterministically RE-QUEUES usage-limit-casualty jobs
(provably never submitted) behind a 3-layer double-apply guard. Expansionary action lives ONLY
here (the Doctor stays conservative-pure; the diagnoser stays advisory-pure). No LLM, $0/pass.

Safety: a job is re-queued only if ALL pass -- (1) its worker has a Tier-0 usage_limit diagnosis,
(2) its dedup_key is NOT in applied_set, (3) NO confirming email_events row for its url. ATS only."""
from __future__ import annotations
from dataclasses import dataclass

REQUEUE_TAG = "requeued_by_remediator:usage_limit"


@dataclass
class Candidate:
    url: str
    worker_id: str
    dedup_key: str | None
    status: str
    attempts: int
    apply_error: str | None
    reason: str


def ensure_remediation_table(conn) -> None:
    """Create the audit/reversal table (additive, idempotent)."""
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS remediation_actions ("
            "  id              BIGSERIAL PRIMARY KEY,"
            "  url             TEXT,"
            "  worker_id       TEXT,"
            "  action          TEXT,"
            "  reason          TEXT,"
            "  prior_status    TEXT,"
            "  prior_attempts  INTEGER,"
            "  prior_apply_error TEXT,"
            "  how_to_reverse  TEXT,"
            "  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_remediation_url ON remediation_actions (url)")
    conn.commit()
```

- [ ] **Step 4: Run to verify it passes**

Run: `.conda-env\python.exe -m pytest tests/test_remediator.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/remediator.py tests/test_remediator.py
git commit -m "feat(remediator): Candidate dataclass + ensure_remediation_table (audit/reversal)"
```

---

### Task 2: Guard 2 (`applied_set`) + Guard 3 (`email_events`) helpers

**Files:**
- Modify: `src/applypilot/fleet/remediator.py`
- Test: `tests/test_remediator.py`

**Interfaces:**
- Consumes: fleet PG conn (Task 1 fakes); a brain SQLite path (real file or `:memory:`).
- Produces: `in_applied_set(conn, dedup_key) -> bool`; `has_confirming_email(brain_path, url) -> bool` (graceful False when the table/file is absent).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_remediator.py`:
```python
import sqlite3


def test_in_applied_set_true_and_false():
    hit = _FakeConn({"FROM applied_set": [{"?column?": 1}]})
    miss = _FakeConn({"FROM applied_set": []})
    assert remediator.in_applied_set(hit, "dk") is True
    assert remediator.in_applied_set(miss, "dk") is False


def test_in_applied_set_none_dedup_key_is_false_without_query():
    conn = _FakeConn({"FROM applied_set": [{"?column?": 1}]})
    assert remediator.in_applied_set(conn, None) is False
    assert conn._cur.executed == []  # short-circuits; never queries on a null key


def test_has_confirming_email_true_when_row_present(tmp_path):
    p = tmp_path / "brain.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE email_events (message_id TEXT PRIMARY KEY, job_url TEXT)")
    c.execute("INSERT INTO email_events VALUES ('m1', 'https://job/1')")
    c.commit(); c.close()
    assert remediator.has_confirming_email(str(p), "https://job/1") is True
    assert remediator.has_confirming_email(str(p), "https://job/2") is False


def test_has_confirming_email_graceful_when_table_absent(tmp_path):
    p = tmp_path / "noet.db"
    sqlite3.connect(p).close()  # valid db, no email_events table
    assert remediator.has_confirming_email(str(p), "https://job/1") is False  # no veto, no crash


def test_has_confirming_email_graceful_when_file_missing(tmp_path):
    assert remediator.has_confirming_email(str(tmp_path / "nope.db"), "u") is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `.conda-env\python.exe -m pytest tests/test_remediator.py -k "applied_set or confirming_email" -v`
Expected: FAIL — attributes not defined.

- [ ] **Step 3: Implement**

Append to `src/applypilot/fleet/remediator.py`:
```python
import sqlite3


def in_applied_set(conn, dedup_key: str | None) -> bool:
    """Guard 2 (internal ground truth): True if this job's dedup_key is already applied."""
    if not dedup_key:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM applied_set WHERE dedup_key=%s LIMIT 1", (dedup_key,))
        return cur.fetchone() is not None


def has_confirming_email(brain_path: str, url: str) -> bool:
    """Guard 3 (external ground truth): True if a recruiter email is tied to this job's url.
    Graceful: a missing brain file or absent email_events table returns False (NO veto), so the
    guarantee never drops below guards 1-2. Read-only."""
    try:
        conn = sqlite3.connect(f"file:{brain_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM email_events WHERE job_url=? LIMIT 1", (url,)).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False  # email_events not created yet (outcomes-tracker not run)
    finally:
        conn.close()
```

- [ ] **Step 4: Run to verify they pass**

Run: `.conda-env\python.exe -m pytest tests/test_remediator.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/remediator.py tests/test_remediator.py
git commit -m "feat(remediator): guard 2 (applied_set) + guard 3 (email_events, graceful) helpers"
```

---

### Task 3: Candidate selection (usage-limit casualties)

**Files:**
- Modify: `src/applypilot/fleet/remediator.py`
- Test: `tests/test_remediator.py`

**Interfaces:**
- Produces: `select_candidates(conn, *, window_minutes=30, max_per_job=2, hard_limit=500) -> list[Candidate]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_remediator.py`:
```python
def test_select_candidates_maps_rows_to_dataclass():
    rows = [{"url": "https://job/1", "worker_id": "m2-3", "dedup_key": "dk1",
             "status": "crash_unconfirmed", "attempts": 99,
             "apply_error": "crash_unconfirmed", "reason": "usage_limit"}]
    conn = _FakeConn({"FROM apply_queue": rows})
    cands = remediator.select_candidates(conn, window_minutes=30, max_per_job=2)
    assert len(cands) == 1
    assert cands[0].url == "https://job/1" and cands[0].reason == "usage_limit"
    # the query must scope to ATS lane, usage_limit diagnoses, and the per-job cap
    sql = conn._cur.executed[0][0]
    assert "lane = 'ats'" in sql and "usage_limit" in sql and "remediation_actions" in sql
```

- [ ] **Step 2: Run to verify it fails**

Run: `.conda-env\python.exe -m pytest tests/test_remediator.py -k select_candidates -v`
Expected: FAIL — attribute not defined.

- [ ] **Step 3: Implement**

Append to `src/applypilot/fleet/remediator.py`:
```python
_CANDIDATE_SQL = """
SELECT q.url, q.worker_id, q.dedup_key, q.status::text AS status, q.attempts,
       q.apply_error, 'usage_limit' AS reason
FROM apply_queue q
JOIN (
    SELECT DISTINCT machine FROM fleet_diagnoses
    WHERE reason = 'usage_limit' AND cluster_key LIKE 'logdiag:%%'
      AND status IN ('recommended', 'open', 'auto_applied')
      AND created_at > now() - make_interval(mins => %(window)s)
) d ON d.machine = q.worker_id
WHERE q.lane = 'ats'
  AND q.status IN ('failed', 'crash_unconfirmed')
  AND (q.status = 'crash_unconfirmed' OR q.apply_error ILIKE '%%no_result_line%%')
  AND q.updated_at > now() - make_interval(mins => %(window)s)
  AND (SELECT count(*) FROM remediation_actions ra
       WHERE ra.url = q.url AND ra.action = 'requeue') < %(maxperjob)s
ORDER BY q.updated_at DESC
LIMIT %(hardlimit)s
"""


def select_candidates(conn, *, window_minutes: int = 30, max_per_job: int = 2,
                      hard_limit: int = 500) -> list[Candidate]:
    """Usage-limit casualties: ATS jobs parked/failed (no_result_line / crash_unconfirmed) by a
    worker that has a recent Tier-0 usage_limit diagnosis, within the diagnosis window, not yet
    re-queued max_per_job times. The double-apply guards run later, per-candidate."""
    with conn.cursor() as cur:
        cur.execute(_CANDIDATE_SQL, {"window": window_minutes, "maxperjob": max_per_job,
                                     "hardlimit": hard_limit})
        return [Candidate(url=r["url"], worker_id=r["worker_id"], dedup_key=r["dedup_key"],
                          status=r["status"], attempts=r["attempts"],
                          apply_error=r["apply_error"], reason=r["reason"])
                for r in cur.fetchall()]
```

- [ ] **Step 4: Run to verify it passes**

Run: `.conda-env\python.exe -m pytest tests/test_remediator.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/remediator.py tests/test_remediator.py
git commit -m "feat(remediator): select_candidates (ATS usage-limit casualties, per-job capped)"
```

---

### Task 4: Re-queue action + audit (reversal)

**Files:**
- Modify: `src/applypilot/fleet/remediator.py`
- Test: `tests/test_remediator.py`

**Interfaces:**
- Produces: `requeue_job(conn, c: Candidate) -> bool` — flips the parked job to `queued` (race-guarded on prior status), writes a `remediation_actions` audit row, commits. Returns True if a row was updated.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_remediator.py`:
```python
class _RowcountCursor(_FakeCursor):
    """Like _FakeCursor but reports rowcount=1 for the UPDATE so requeue_job sees success."""
    def __init__(self, script):
        super().__init__(script); self.rowcount = 0
    def execute(self, sql, params=None):
        super().execute(sql, params)
        self.rowcount = 1 if sql.strip().upper().startswith("UPDATE") else 0


class _RowcountConn(_FakeConn):
    def __init__(self, script=None):
        super().__init__(script); self._cur = _RowcountCursor(script or {})


def test_requeue_job_updates_to_queued_and_audits():
    conn = _RowcountConn()
    c = remediator.Candidate(url="https://job/1", worker_id="m2-3", dedup_key="dk1",
                             status="crash_unconfirmed", attempts=99,
                             apply_error="crash_unconfirmed", reason="usage_limit")
    assert remediator.requeue_job(conn, c) is True and conn.committed is True
    upd = [e for e in conn._cur.executed if e[0].strip().upper().startswith("UPDATE apply_queue")]
    ins = [e for e in conn._cur.executed if "INSERT INTO remediation_actions" in e[0]]
    assert len(upd) == 1 and len(ins) == 1
    assert "status='queued'" in upd[0][0].replace(" ", "") or "status = 'queued'" in upd[0][0]
    # audit row carries the PRIOR state for reversal
    assert "crash_unconfirmed" in ins[0][1] and 99 in ins[0][1]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.conda-env\python.exe -m pytest tests/test_remediator.py -k requeue_job -v`
Expected: FAIL — attribute not defined.

- [ ] **Step 3: Implement**

Append to `src/applypilot/fleet/remediator.py`:
```python
def requeue_job(conn, c: Candidate) -> bool:
    """Reverse the reclaim park for ONE proven-never-submitted job: status -> 'queued', attempts
    -> 0, lease cleared, apply_error tagged. Race-guarded on the prior status. Writes a reversal
    audit row. Caller MUST have passed all 3 guards before calling this. Returns True if updated."""
    how_to_reverse = (f"UPDATE apply_queue SET status='{c.status}', attempts={c.attempts}, "
                      f"apply_error={c.apply_error!r} WHERE url={c.url!r};")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE apply_queue "
            "SET status='queued'::apply_queue_status, attempts=0, lease_owner=NULL, "
            "    lease_expires_at=NULL, apply_error=%(tag)s, updated_at=now() "
            "WHERE url=%(url)s AND status=%(prior)s::apply_queue_status",
            {"tag": REQUEUE_TAG, "url": c.url, "prior": c.status})
        if cur.rowcount != 1:
            return False  # status changed since selection (race) -> do nothing
        cur.execute(
            "INSERT INTO remediation_actions (url, worker_id, action, reason, prior_status, "
            "prior_attempts, prior_apply_error, how_to_reverse) "
            "VALUES (%s,%s,'requeue',%s,%s,%s,%s,%s)",
            (c.url, c.worker_id, c.reason, c.status, c.attempts, c.apply_error, how_to_reverse))
    conn.commit()
    return True
```

- [ ] **Step 4: Run to verify it passes**

Run: `.conda-env\python.exe -m pytest tests/test_remediator.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/remediator.py tests/test_remediator.py
git commit -m "feat(remediator): requeue_job (reverse the park, race-guarded, audited/reversible)"
```

---

### Task 5: Orchestrator — 3-guard gate + caps + recommendations

**Files:**
- Modify: `src/applypilot/fleet/remediator.py`
- Test: `tests/test_remediator.py`

**Interfaces:**
- Produces: `remediate(conn, *, brain_path=None, max_requeue=50, max_per_job=2, window_minutes=30) -> dict` returning counts `{"requeued","vetoed_applied_set","vetoed_email","capped","candidates"}`. Uses module-level seams `select_candidates`, `in_applied_set`, `has_confirming_email`, `requeue_job` (so tests monkeypatch them).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_remediator.py`:
```python
def _cand(url, dk="dk"):
    return remediator.Candidate(url=url, worker_id="m2-3", dedup_key=dk,
                                status="crash_unconfirmed", attempts=99,
                                apply_error="crash_unconfirmed", reason="usage_limit")


def test_remediate_applies_guards_and_caps(monkeypatch):
    cands = [_cand("u-clean"), _cand("u-applied"), _cand("u-emailed"), _cand("u-overflow")]
    monkeypatch.setattr(remediator, "ensure_remediation_table", lambda conn: None)
    monkeypatch.setattr(remediator, "select_candidates", lambda conn, **k: cands)
    # guard 2 vetoes the candidate whose dedup_key == "applied"; guard 3 vetoes url "u-emailed"
    monkeypatch.setattr(remediator, "in_applied_set",
                        lambda conn, dk: dk == "applied")
    monkeypatch.setattr(remediator, "has_confirming_email",
                        lambda bp, url: url == "u-emailed")
    requeued = []
    monkeypatch.setattr(remediator, "requeue_job",
                        lambda conn, c: (requeued.append(c.url) or True))
    cands[1] = _cand("u-applied", dk="applied")  # guard-2 veto target
    out = remediator.remediate(object(), brain_path="x", max_requeue=1, max_per_job=2)
    # only u-clean re-queued (u-applied vetoed, u-emailed vetoed, then max_requeue=1 caps the rest)
    assert requeued == ["u-clean"]
    assert out["requeued"] == 1 and out["vetoed_applied_set"] == 1 and out["vetoed_email"] == 1
    assert out["capped"] >= 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `.conda-env\python.exe -m pytest tests/test_remediator.py -k remediate -v`
Expected: FAIL — attribute not defined.

- [ ] **Step 3: Implement**

Append to `src/applypilot/fleet/remediator.py`:
```python
def remediate(conn, *, brain_path: str | None = None, max_requeue: int = 50,
              max_per_job: int = 2, window_minutes: int = 30) -> dict:
    """One pass: select usage-limit casualties, then re-queue each ONLY if all 3 guards pass,
    bounded by max_requeue. Guard failures and cap overflow are left parked (a recommendation,
    not an action). Returns a summary. brain_path defaults to the live brain (config.DB_PATH)."""
    if brain_path is None:
        from applypilot.config import DB_PATH
        brain_path = str(DB_PATH)
    ensure_remediation_table(conn)
    cands = select_candidates(conn, window_minutes=window_minutes, max_per_job=max_per_job)
    out = {"candidates": len(cands), "requeued": 0, "vetoed_applied_set": 0,
           "vetoed_email": 0, "capped": 0}
    for c in cands:
        if in_applied_set(conn, c.dedup_key):           # guard 2
            out["vetoed_applied_set"] += 1
            continue
        if has_confirming_email(brain_path, c.url):     # guard 3
            out["vetoed_email"] += 1
            continue
        if out["requeued"] >= max_requeue:              # per-pass blast-radius cap
            out["capped"] += 1
            continue
        if requeue_job(conn, c):                        # guard 1 already satisfied by selection
            out["requeued"] += 1
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `.conda-env\python.exe -m pytest tests/test_remediator.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/remediator.py tests/test_remediator.py
git commit -m "feat(remediator): remediate() orchestrator (3-guard gate + per-pass cap + summary)"
```

---

### Task 6: CLI `applypilot-fleet-remediate`

**Files:**
- Create: `src/applypilot/fleet/remediator_main.py`
- Modify: `pyproject.toml` (`[project.scripts]` — one line)
- Test: `tests/test_remediator_cli.py`

**Interfaces:**
- Consumes: `applypilot.apply.pgqueue.connect`, `remediator.remediate`.
- Produces: `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_remediator_cli.py`:
```python
from applypilot.fleet import remediator_main, remediator


class _Conn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_cli_once_runs_remediate_and_prints(monkeypatch, capsys):
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn=None: _Conn())
    monkeypatch.setattr(remediator, "remediate",
                        lambda conn, **k: {"candidates": 3, "requeued": 2,
                                           "vetoed_applied_set": 1, "vetoed_email": 0, "capped": 0})
    rc = remediator_main.main(["--once", "--dsn", "x"])
    assert rc == 0
    assert "requeued" in capsys.readouterr().out.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.conda-env\python.exe -m pytest tests/test_remediator_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: ...remediator_main`.

- [ ] **Step 3: Implement the CLI**

Create `src/applypilot/fleet/remediator_main.py`:
```python
"""applypilot-fleet-remediate: autonomously re-queue usage-limit-casualty jobs behind the 3-layer
double-apply guard. --once for a single pass; --interval to loop. ATS only, bounded, reversible."""
from __future__ import annotations
import argparse
import sys
import time

from applypilot.apply import pgqueue
from applypilot.fleet import remediator


def _one_pass(args) -> None:
    with pgqueue.connect(args.dsn) as conn:
        out = remediator.remediate(conn, max_requeue=args.max_requeue,
                                   max_per_job=args.max_per_job, window_minutes=args.window_minutes)
    print(f"[remediate] candidates={out['candidates']} requeued={out['requeued']} "
          f"vetoed_applied_set={out['vetoed_applied_set']} vetoed_email={out['vetoed_email']} "
          f"capped={out['capped']}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="applypilot-fleet-remediate",
        description="Re-queue usage-limit-casualty jobs (3-guard double-apply gate). ATS only.")
    p.add_argument("--dsn", default=None,
                   help="Postgres DSN (default: DATABASE_URL / APPLYPILOT_FLEET_DSN env).")
    p.add_argument("--max-requeue", type=int, default=50, dest="max_requeue",
                   help="per-pass blast-radius cap (default 50)")
    p.add_argument("--max-per-job", type=int, default=2, dest="max_per_job",
                   help="max re-queues per job ever (default 2)")
    p.add_argument("--window-minutes", type=int, default=30, dest="window_minutes")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="single pass")
    g.add_argument("--interval", type=int, help="loop every N seconds")
    args = p.parse_args(argv)

    if args.once:
        _one_pass(args)
        return 0
    while True:                       # --interval loop
        _one_pass(args)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Add the console-script + reinstall**

In `pyproject.toml`, in the `[project.scripts]` block (next to `applypilot-fleet-diagnose`), add:
```toml
applypilot-fleet-remediate = "applypilot.fleet.remediator_main:main"
```
Then: `.conda-env\python.exe -m pip install -e . -q`

- [ ] **Step 5: Run to verify it passes**

Run: `.conda-env\python.exe -m pytest tests/test_remediator_cli.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/applypilot/fleet/remediator_main.py tests/test_remediator_cli.py pyproject.toml
git commit -m "feat(remediator): applypilot-fleet-remediate CLI (--once / --interval)"
```

---

### Task 7: Standalone launcher (ops folder)

**Files:**
- Create: `C:\Users\JStal\OneDrive\Documents\ApplyPilot-ops\run-fleet-remediate.ps1`

> No unit test (operational launcher). Verification is a manual `--once` dry run.

- [ ] **Step 1: Create the launcher**

Create `C:\Users\JStal\OneDrive\Documents\ApplyPilot-ops\run-fleet-remediate.ps1`:
```powershell
# run-fleet-remediate.ps1 [-Once] [-Interval 300] [-MaxRequeue 50] [-MaxPerJob 2]
#   Autonomous fleet REMEDIATOR (home box): re-queues usage-limit-casualty jobs (provably never
#   submitted) behind a 3-layer double-apply guard (usage_limit diagnosis + applied_set + email).
#   ATS lane only; bounded; every re-queue is audited + reversible in remediation_actions.
param([switch]$Once, [int]$Interval = 300, [int]$MaxRequeue = 50, [int]$MaxPerJob = 2)
$ErrorActionPreference = "Stop"
$exe = $null
foreach ($d in @("C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot\.conda-env\Scripts")) {
  $cand = Join-Path $d "applypilot-fleet-remediate.exe"
  if (Test-Path $cand) { $exe = $cand; break }
}
if (-not $exe) { throw "applypilot-fleet-remediate not found -- run pip install -e . in the repo." }
if (-not $env:FLEET_PG_DSN) {
  $env:FLEET_PG_DSN = "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
}
$env:APPLYPILOT_FLEET_DSN = $env:FLEET_PG_DSN
$env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"
$mode = if ($Once) { @("--once") } else { @("--interval", $Interval) }
Write-Host "[remediate] $(if($Once){'single pass'}else{"loop ${Interval}s"})  max-requeue=$MaxRequeue  max-per-job=$MaxPerJob"
& $exe --dsn $env:FLEET_PG_DSN --max-requeue $MaxRequeue --max-per-job $MaxPerJob @mode
```

- [ ] **Step 2: Manual verification (dry, single pass)**

Run: `powershell -ExecutionPolicy Bypass -File "C:\Users\JStal\OneDrive\Documents\ApplyPilot-ops\run-fleet-remediate.ps1" -Once -MaxRequeue 5`
Expected: prints a `[remediate] candidates=… requeued=…` summary; confirm via
`psql ... -c "select url, prior_status, how_to_reverse from remediation_actions order by created_at desc limit 5;"`.

- [ ] **Step 3: Commit** *(ops folder is outside the repo; nothing to commit. If a repo copy is later desired, add it then.)*

---

## Self-Review

- **Spec coverage:** 3-layer guard (Tasks 2+5) ✓ · candidate selection / usage-limit window (Task 3) ✓ · re-queue reverses the park + reversal audit (Tasks 1+4) ✓ · per-pass + per-job caps (Tasks 3+5) ✓ · ATS-only / no-LinkedIn (Task 3 SQL `lane='ats'`) ✓ · graceful email_events (Task 2) ✓ · CLI --once/--interval (Task 6) ✓ · standalone launcher (Task 7) ✓ · deterministic/no-LLM (whole plan) ✓.
- **Placeholder scan:** every step has complete code + exact commands/expected output; no TBD/TODO.
- **Type consistency:** `Candidate` fields (url, worker_id, dedup_key, status, attempts, apply_error, reason) are identical across Tasks 1/3/4/5; `remediate`/`select_candidates`/`requeue_job`/`in_applied_set`/`has_confirming_email` signatures match between definition and the orchestrator's seams; `remediate` returns the same key set the CLI prints.
- **Safety:** guard 1 = selection (reason='usage_limit'); guards 2+3 hard-veto before any re-queue; `requeue_job` is race-guarded on prior status; `max_requeue`/`max_per_job` bound the blast radius; every action is reversible via `remediation_actions`.
