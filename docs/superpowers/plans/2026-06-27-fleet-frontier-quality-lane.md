# Fleet Frontier Quality Lane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A home-side `applypilot-fleet-frontier` pass that re-scores the contested/high-value backlog with a frontier model — the user's Codex Pro subscription (gpt‑5.5) as the priority backend, a metered frontier API as failover — and records frontier scores + agreement-vs-cheap in an advisory `frontier_scores` side-table with a disagreement report.

**Architecture:** Pure home-side Python over the SQLite brain (`config.DB_PATH`). A priority selector picks the backlog; an orchestrator scores each job serial+governed, choosing the Codex model by tier and failing over to a metered API on any limit; results land in an advisory side-table. The frontier backend is a subprocess (`codex exec`) wrapped so a malformed/limited call raises `SubscriptionUnavailable` and the orchestrator fails over. Nothing here touches the shipped compute lane, the fleet Postgres, or the `jobs` table.

**Tech Stack:** Python 3.11 (`.conda-env`), stdlib `sqlite3` + `subprocess`, pytest (temp SQLite + monkeypatch — NO Postgres). Reuses `scoring/scorer.py` (the real scorer) and `codex exec` (verified flags).

## Global Constraints

- Run tests with `.conda-env/python.exe -m pytest <path> -q` from the repo root (`C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot`). These tests use **temp SQLite + monkeypatch only — no `fleet_db`/Postgres fixture**.
- **Commit only the specific paths named in each task — NEVER `git add -A`.** The user's 7 dirty files (`run-applypilot.ps1`, `discovery/jobspy.py`, `pipeline.py`, `scoring/cover_letter.py`, `scoring/tailor.py`, `tests/test_discovery_scheduler.py`, `tests/test_generation_workers.py`) and sibling-session files must stay untouched.
- Allowed upstream touch-point: `src/applypilot/scoring/scorer.py` (add a prompt-text helper — Task 1). Everything else new lives under `src/applypilot/fleet/` and `tests/`.
- **Advisory only:** the frontier pass writes a `frontier_scores` side-table; it must NEVER write `jobs.fit_score` / `jobs.audit_score` / `jobs.research_fit_score`.
- **`codex exec` argv (verified):** `codex exec -m <MODEL> --output-schema <SCHEMA_FILE> -o <OUT_FILE> "<PROMPT>"`. Parse `<OUT_FILE>` (the final message) as the schema JSON. Never use `--json`.
- Subscription backend is **default-off in code paths that auto-run**: the orchestrator only uses `codex-subscription` when `use_subscription=True` is explicitly passed.

## File Structure

- Create `src/applypilot/fleet/frontier_db.py` — `ensure_frontier_schema`, `upsert_frontier_score`, `disagreement_report` (the side-table + its reads).
- Create `src/applypilot/fleet/frontier_select.py` — `select_priority`.
- Create `src/applypilot/fleet/cli_providers.py` — `SubscriptionUnavailable`, `score_via_codex` (+ a stubbed `score_via_claude` for the optional cross-check, Task 8).
- Create `src/applypilot/fleet/frontier_governor.py` — `FrontierGovernor`.
- Create `src/applypilot/fleet/frontier_pass.py` — `run_frontier_pass` (orchestrator).
- Create `src/applypilot/fleet/frontier_main.py` — `build_and_run`, `main` (`applypilot-fleet-frontier`).
- Modify `src/applypilot/scoring/scorer.py` — add `build_score_prompt_text`.
- Modify `pyproject.toml` — register the console script.
- Tests: `tests/test_frontier_db.py`, `tests/test_frontier_select.py`, `tests/test_cli_providers.py`, `tests/test_frontier_governor.py`, `tests/test_frontier_pass.py`, `tests/test_frontier_main.py`.

---

### Task 1: `build_score_prompt_text` (scorer helper)

**Files:**
- Modify: `src/applypilot/scoring/scorer.py`
- Test: `tests/test_build_score_prompt_text.py`

**Interfaces:**
- Produces:
  - `scorer.build_score_prompt_text(resume_text, job, preference_profile=None, knowledge_graph_prompt=None) -> str` — the combined system+user prompt text the CLI backend feeds to `codex exec` (so the subprocess scores with the SAME instructions as the API path). `job` keys: `title`, `site`, `location`, `full_description`.
  - `scorer.load_score_context() -> dict` — `{resume_text, preference_profile, kg_prompt}` from the SAME sources `run_scoring` reads (`RESUME_PATH`, `load_preference_profile()`, `KNOWLEDGE_GRAPH_PROMPT_PATH` with the `_MAX_KNOWLEDGE_GRAPH_CHARS` truncation). Used by the frontier CLI so it doesn't reinvent context loading.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_build_score_prompt_text.py
from applypilot.scoring import scorer


def test_build_score_prompt_text_includes_resume_job_and_instructions():
    job = {"title": "Chief of Staff", "site": "Acme", "location": "Remote",
           "full_description": "Lead operations and strategy."}
    text = scorer.build_score_prompt_text("RESUME-OPS-LEADER", job, knowledge_graph_prompt="KG-PACK")
    assert isinstance(text, str)
    assert "RESUME-OPS-LEADER" in text
    assert "Chief of Staff" in text and "Acme" in text and "Lead operations" in text
    assert "KG-PACK" in text
    # carries the scoring instruction (the SCORE_PROMPT system text)
    assert "fit" in text.lower() and "score" in text.lower()
```

- [ ] **Step 2: Run it, expect FAIL** — `.conda-env/python.exe -m pytest tests/test_build_score_prompt_text.py -q` (AttributeError).

- [ ] **Step 3: Implement** — in `scorer.py`, add (factoring the job-text + user-parts assembly already present in `score_job`):

```python
def build_score_prompt_text(resume_text, job, preference_profile=None, knowledge_graph_prompt=None) -> str:
    """The combined prompt text for a single-string backend (e.g. `codex exec`).
    Same instructions + context as score_job, flattened to one prompt."""
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job.get('site', '')}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )
    parts = [SCORE_PROMPT, f"RESUME:\n{resume_text}"]
    pref = _preference_profile_prompt(preference_profile)
    if pref:
        parts.append(pref)
    if knowledge_graph_prompt:
        parts.append(knowledge_graph_prompt)
    parts.append(f"JOB POSTING:\n{job_text}")
    return "\n\n---\n\n".join(parts)
```

- [ ] **Step 3b: Add `load_score_context` (same file)** — factor the context sources `run_scoring` already reads (do NOT change `run_scoring`; this reads the same paths):

```python
def load_score_context() -> dict:
    """Resume / preference profile / KG prompt from the same sources run_scoring reads.
    For the frontier pass + any standalone scorer caller."""
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    preference_profile = load_preference_profile()
    kg_prompt = None
    try:
        if KNOWLEDGE_GRAPH_PROMPT_PATH.exists():
            kg_prompt = KNOWLEDGE_GRAPH_PROMPT_PATH.read_text(encoding="utf-8")
            if kg_prompt and len(kg_prompt) > _MAX_KNOWLEDGE_GRAPH_CHARS:
                kg_prompt = kg_prompt[:_MAX_KNOWLEDGE_GRAPH_CHARS] + "\n...[truncated]"
            if not (kg_prompt or "").strip():
                kg_prompt = None
    except OSError:
        kg_prompt = None
    return {"resume_text": resume_text, "preference_profile": preference_profile, "kg_prompt": kg_prompt}
```

Add a test (monkeypatching the module paths so it's hermetic):

```python
def test_load_score_context(monkeypatch, tmp_path):
    r = tmp_path / "resume.txt"; r.write_text("RESUME", encoding="utf-8")
    k = tmp_path / "kg.txt"; k.write_text("KG", encoding="utf-8")
    monkeypatch.setattr(scorer, "RESUME_PATH", r)
    monkeypatch.setattr(scorer, "KNOWLEDGE_GRAPH_PROMPT_PATH", k)
    monkeypatch.setattr(scorer, "load_preference_profile", lambda: {"summary": "ops"})
    ctx = scorer.load_score_context()
    assert ctx["resume_text"] == "RESUME" and ctx["kg_prompt"] == "KG"
    assert ctx["preference_profile"] == {"summary": "ops"}
```

- [ ] **Step 4: Run it, expect PASS** — `.conda-env/python.exe -m pytest tests/test_build_score_prompt_text.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/scoring/scorer.py tests/test_build_score_prompt_text.py
git commit -m "feat(scorer): build_score_prompt_text + load_score_context helpers"
```

---

### Task 2: `frontier_db.py` — the advisory side-table

**Files:**
- Create: `src/applypilot/fleet/frontier_db.py`
- Test: `tests/test_frontier_db.py`

**Interfaces:**
- Produces:
  - `ensure_frontier_schema(conn) -> None` — idempotently create `frontier_scores`.
  - `upsert_frontier_score(conn, *, url, cheap_score, frontier_score, provider, agreement, frontier_decision=None, opus_score=None, reasoning=None) -> None`.
  - `disagreement_report(conn, *, max_agreement=0.8) -> list[dict]` — rows with `agreement < max_agreement`, ordered by agreement asc.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_frontier_db.py
import sqlite3
from applypilot.fleet import frontier_db as fdb


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    fdb.ensure_frontier_schema(c)
    return c


def test_upsert_and_disagreement_report():
    c = _conn()
    fdb.upsert_frontier_score(c, url="a", cheap_score=8.0, frontier_score=8.0, provider="gpt-5.5", agreement=1.0)
    fdb.upsert_frontier_score(c, url="b", cheap_score=7.0, frontier_score=3.0, provider="gpt-5.5", agreement=0.56)
    fdb.upsert_frontier_score(c, url="c", cheap_score=9.0, frontier_score=5.0, provider="gpt-5.5", agreement=0.55)
    # upsert is idempotent on url
    fdb.upsert_frontier_score(c, url="a", cheap_score=8.0, frontier_score=8.0, provider="gpt-5.5", agreement=1.0)
    assert c.execute("SELECT COUNT(*) FROM frontier_scores").fetchone()[0] == 3
    rep = fdb.disagreement_report(c, max_agreement=0.8)
    assert [r["url"] for r in rep] == ["c", "b"]  # ordered by agreement asc, only < 0.8
```

- [ ] **Step 2: Run it, expect FAIL** (ModuleNotFound).

- [ ] **Step 3: Implement**

```python
# src/applypilot/fleet/frontier_db.py
"""Advisory frontier-score side-table in the brain (no jobs migration). The frontier
quality lane writes here ONLY -- jobs.fit_score/audit_score/research_fit_score are
never touched."""
from __future__ import annotations

from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS frontier_scores (
  url            TEXT PRIMARY KEY,
  cheap_score    REAL,
  frontier_score REAL,
  opus_score     REAL,
  frontier_decision TEXT,
  provider       TEXT,
  agreement      REAL,
  reasoning      TEXT,
  scored_at      TEXT
);
"""


def ensure_frontier_schema(conn) -> None:
    conn.execute(_SCHEMA)
    conn.commit()


def upsert_frontier_score(conn, *, url, cheap_score, frontier_score, provider, agreement,
                          frontier_decision=None, opus_score=None, reasoning=None) -> None:
    conn.execute(
        "INSERT INTO frontier_scores "
        "(url, cheap_score, frontier_score, opus_score, frontier_decision, provider, agreement, reasoning, scored_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET cheap_score=excluded.cheap_score, frontier_score=excluded.frontier_score, "
        "opus_score=excluded.opus_score, frontier_decision=excluded.frontier_decision, provider=excluded.provider, "
        "agreement=excluded.agreement, reasoning=excluded.reasoning, scored_at=excluded.scored_at",
        (url, cheap_score, frontier_score, opus_score, frontier_decision, provider, agreement, reasoning,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def disagreement_report(conn, *, max_agreement=0.8) -> list[dict]:
    rows = conn.execute(
        "SELECT f.url, j.company, j.title, f.cheap_score, f.frontier_score, f.opus_score, f.agreement, f.provider "
        "FROM frontier_scores f LEFT JOIN jobs j ON j.url = f.url "
        "WHERE f.agreement < ? ORDER BY f.agreement ASC",
        (max_agreement,),
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run it, expect PASS.** (The `LEFT JOIN jobs` tolerates a missing `jobs` table only if it exists; the test creates just `frontier_scores`, so add a minimal `jobs` table in the test OR keep the join `LEFT` and create `jobs` in the test.) Update the test's `_conn` to also `CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT)` so the join resolves.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/frontier_db.py tests/test_frontier_db.py
git commit -m "feat(fleet): frontier_scores advisory side-table + disagreement report"
```

---

### Task 3: `frontier_select.py` — priority backlog selector

**Files:**
- Create: `src/applypilot/fleet/frontier_select.py`
- Test: `tests/test_frontier_select.py`

**Interfaces:**
- Consumes: `frontier_db.ensure_frontier_schema`.
- Produces: `select_priority(conn, *, limit=200, floor=7.0, mode="backlog", hours=24, urls=None) -> list[dict]` (`{url, company, title, full_description, cheap_score}`), `cheap_score = COALESCE(research_fit_score, fit_score)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_frontier_select.py
import sqlite3
from applypilot.fleet import frontier_select as fs, frontier_db as fdb

_DDL = """CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, full_description TEXT,
  fit_score INTEGER, research_fit_score REAL, duplicate_of_url TEXT, discovered_at TEXT);"""


def _brain():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.executescript(_DDL); fdb.ensure_frontier_schema(c); return c


def test_backlog_orders_by_cheap_score_respects_floor_and_exclusions():
    c = _brain()
    c.executemany("INSERT INTO jobs (url, company, title, full_description, research_fit_score) VALUES (?,?,?,?,?)",
                  [("u9", "C", "COS", "d", 9.0), ("u7", "C", "Analyst", "d", 7.0), ("u5", "C", "PM", "d", 5.0)])
    c.execute("INSERT INTO jobs (url, company, title, fit_score, duplicate_of_url) VALUES ('udup','C','X',9,'u9')")
    fdb.upsert_frontier_score(c, url="u9", cheap_score=9.0, frontier_score=9.0, provider="m", agreement=1.0)  # already done
    c.commit()
    got = [r["url"] for r in fs.select_priority(c, floor=7.0, limit=10)]
    assert got == ["u7"]  # u9 already-frontier-scored, u5 below floor, udup is a duplicate
```

- [ ] **Step 2: Run it, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# src/applypilot/fleet/frontier_select.py
"""Priority backlog selector for the frontier quality lane: the highest cheap-scored
jobs not yet frontier-scored. Reads the brain; writes nothing."""
from __future__ import annotations

from applypilot.fleet import frontier_db

_BASE = """
SELECT j.url, j.company, j.title, j.full_description,
       COALESCE(j.research_fit_score, j.fit_score) AS cheap_score
FROM jobs j
LEFT JOIN frontier_scores f ON f.url = j.url
WHERE f.url IS NULL
  AND j.duplicate_of_url IS NULL
  AND COALESCE(j.research_fit_score, j.fit_score) >= ?
"""


def select_priority(conn, *, limit=200, floor=7.0, mode="backlog", hours=24, urls=None) -> list[dict]:
    frontier_db.ensure_frontier_schema(conn)
    if mode == "urls":
        marks = ",".join("?" * len(urls or []))
        sql = _BASE + f" AND j.url IN ({marks}) ORDER BY cheap_score DESC LIMIT ?"
        params = [floor, *(urls or []), limit]
    elif mode == "new":
        sql = _BASE + " AND j.discovered_at >= datetime('now', ?) ORDER BY cheap_score DESC LIMIT ?"
        params = [floor, f"-{int(hours)} hours", limit]
    else:  # backlog
        sql = _BASE + " ORDER BY cheap_score DESC LIMIT ?"
        params = [floor, limit]
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
```

- [ ] **Step 4: Run it, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/frontier_select.py tests/test_frontier_select.py
git commit -m "feat(fleet): frontier priority backlog selector"
```

---

### Task 4: `cli_providers.py` — `score_via_codex`

**Files:**
- Create: `src/applypilot/fleet/cli_providers.py`
- Test: `tests/test_cli_providers.py`

**Interfaces:**
- Produces: `SubscriptionUnavailable(Exception)`; `score_via_codex(prompt, *, schema_path, model=None, timeout_s=120, retries=2, _runner=subprocess.run) -> dict`. Runs `codex exec [-m model] --output-schema schema_path -o <tmp> "prompt"`, parses `<tmp>` as JSON; retries on malformed; raises `SubscriptionUnavailable` on non-zero exit / parse-exhaustion. `_runner` is injectable for tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_providers.py
import json
import subprocess
import pytest
from applypilot.fleet import cli_providers as clp


class _FakeProc:
    def __init__(self, returncode=0): self.returncode = returncode; self.stdout = ""; self.stderr = ""


def _runner_writing(obj, returncode=0):
    def run(argv, **kw):
        # find the -o output file and write the (maybe malformed) content
        out = argv[argv.index("-o") + 1]
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj))
        return _FakeProc(returncode)
    return run


def test_score_via_codex_parses_and_passes_flags(tmp_path):
    seen = {}
    def run(argv, **kw):
        seen["argv"] = argv
        with open(argv[argv.index("-o") + 1], "w", encoding="utf-8") as fh:
            fh.write('{"score": 8, "reasoning": "fit"}')
        return _FakeProc(0)
    out = clp.score_via_codex("PROMPT", schema_path=str(tmp_path / "s.json"), model="gpt-5.5", _runner=run)
    assert out["score"] == 8
    a = seen["argv"]
    assert a[:2] == ["codex", "exec"] and "-m" in a and "gpt-5.5" in a
    assert "--output-schema" in a and "-o" in a and "--json" not in a


def test_score_via_codex_retries_then_raises_on_malformed(tmp_path):
    out = _runner_writing("not json", 0)
    with pytest.raises(clp.SubscriptionUnavailable):
        clp.score_via_codex("P", schema_path=str(tmp_path / "s.json"), retries=2, _runner=out)


def test_score_via_codex_raises_on_nonzero_exit(tmp_path):
    with pytest.raises(clp.SubscriptionUnavailable):
        clp.score_via_codex("P", schema_path=str(tmp_path / "s.json"), _runner=_runner_writing({}, returncode=1))
```

- [ ] **Step 2: Run it, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# src/applypilot/fleet/cli_providers.py
"""Subscription-CLI scoring backends (Flavor B). score_via_codex shells out to the
user's logged-in Codex (ChatGPT subscription) on the home box. Holds no token.
On any limit/auth/parse failure it raises SubscriptionUnavailable so the caller
(frontier_pass) can fail over to a metered API."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


class SubscriptionUnavailable(Exception):
    """The subscription CLI couldn't produce a usable score (limit/auth/exit/parse)."""


def score_via_codex(prompt, *, schema_path, model=None, timeout_s=120, retries=2, _runner=subprocess.run) -> dict:
    last = None
    for _ in range(max(1, retries)):
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "last.txt")
            argv = ["codex", "exec"]
            if model:
                argv += ["-m", model]
            argv += ["--output-schema", schema_path, "-o", out, prompt]
            try:
                proc = _runner(argv, capture_output=True, text=True, timeout=timeout_s)
            except Exception as e:  # transport / timeout
                raise SubscriptionUnavailable(f"codex exec failed: {e}") from e
            if getattr(proc, "returncode", 1) != 0:
                raise SubscriptionUnavailable(f"codex exec exit {getattr(proc, 'returncode', '?')}: {getattr(proc, 'stderr', '')[:200]}")
            try:
                text = Path(out).read_text(encoding="utf-8")
                data = json.loads(text)
                if "score" in data:
                    return data
                last = "no score key"
            except (ValueError, OSError) as e:
                last = str(e)
        prompt = prompt + "\n\nReturn ONLY the JSON object conforming to the schema."
    raise SubscriptionUnavailable(f"codex exec produced no valid score after retries: {last}")
```

- [ ] **Step 4: Run it, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/cli_providers.py tests/test_cli_providers.py
git commit -m "feat(fleet): score_via_codex subscription backend with failover signal"
```

---

### Task 5: `frontier_governor.py`

**Files:**
- Create: `src/applypilot/fleet/frontier_governor.py`
- Test: `tests/test_frontier_governor.py`

**Interfaces:**
- Produces: `FrontierGovernor(account, *, min_gap_seconds=0.0, window_seconds=None, window_budget=None, state_path=None, _now=time.monotonic)`; `allow() -> bool`; `record(outcome, *, now=None) -> None` (`outcome in {'ok','limit'}`); `'limit'` trips the account out for `window_seconds` (or a default).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_frontier_governor.py
from applypilot.fleet.frontier_governor import FrontierGovernor


def test_allow_min_gap_and_limit_trip():
    t = {"v": 1000.0}
    g = FrontierGovernor("codex", min_gap_seconds=10, window_seconds=300, _now=lambda: t["v"])
    assert g.allow() is True
    g.record("ok")
    assert g.allow() is False              # within min-gap
    t["v"] += 11
    assert g.allow() is True               # past the gap
    g.record("limit")                       # tripped for the window
    assert g.allow() is False
    t["v"] += 301
    assert g.allow() is True                # window elapsed -> recovered


def test_window_budget_optional_bound():
    t = {"v": 0.0}
    g = FrontierGovernor("codex", min_gap_seconds=0, window_seconds=100, window_budget=2, _now=lambda: t["v"])
    g.record("ok"); g.record("ok")
    assert g.allow() is False               # budget spent this window
    t["v"] += 101
    assert g.allow() is True                # window rolled
```

- [ ] **Step 2: Run it, expect FAIL.**

- [ ] **Step 3: Implement** (in-memory state; `state_path` JSON persistence optional — implement the in-memory core the tests cover; persist on `record` if `state_path` set):

```python
# src/applypilot/fleet/frontier_governor.py
"""Reactive subscription rate-governor (home-side). Spend is not a constraint; this
exists to (a) not hammer after a limit signal (the abuse pattern), (b) pace serial
calls, (c) optionally bound a window when sharing a dev account."""
from __future__ import annotations

import json
import time


class FrontierGovernor:
    def __init__(self, account, *, min_gap_seconds=0.0, window_seconds=None,
                 window_budget=None, state_path=None, _now=time.monotonic):
        self.account = account
        self.min_gap = float(min_gap_seconds)
        self.window_seconds = window_seconds
        self.window_budget = window_budget
        self.state_path = state_path
        self._now = _now
        self._last_call = None
        self._tripped_until = None
        self._window_start = self._now()
        self._window_count = 0
        self._load()

    def _roll(self):
        if self.window_seconds and (self._now() - self._window_start) >= self.window_seconds:
            self._window_start = self._now()
            self._window_count = 0

    def allow(self) -> bool:
        self._roll()
        now = self._now()
        if self._tripped_until is not None:
            if now < self._tripped_until:
                return False
            self._tripped_until = None
        if self._last_call is not None and (now - self._last_call) < self.min_gap:
            return False
        if self.window_budget is not None and self._window_count >= self.window_budget:
            return False
        return True

    def record(self, outcome, *, now=None) -> None:
        t = now if now is not None else self._now()
        self._last_call = t
        self._roll()
        self._window_count += 1
        if outcome == "limit":
            self._tripped_until = t + float(self.window_seconds or 300)
        self._save()

    def _load(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                s = json.load(fh)
            self._tripped_until = s.get("tripped_until")
        except (OSError, ValueError):
            pass

    def _save(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump({"tripped_until": self._tripped_until}, fh)
        except OSError:
            pass
```

- [ ] **Step 4: Run it, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/frontier_governor.py tests/test_frontier_governor.py
git commit -m "feat(fleet): reactive frontier rate-governor"
```

---

### Task 6: `frontier_pass.py` — orchestrator

**Files:**
- Create: `src/applypilot/fleet/frontier_pass.py`
- Test: `tests/test_frontier_pass.py`

**Interfaces:**
- Consumes: `frontier_select.select_priority`, `cli_providers.score_via_codex`/`SubscriptionUnavailable`, `frontier_governor.FrontierGovernor`, `scorer.score_job`/`build_score_prompt_text`, `frontier_db.upsert_frontier_score`.
- Produces: `run_frontier_pass(conn, *, resume_text, preference_profile=None, kg_prompt=None, limit=200, floor=7.0, mode="backlog", hours=24, urls=None, use_subscription=True, metered_provider="gpt-5.5", top_model="gpt-5.5", backlog_model="gpt-5.5", top_tier_floor=8.5, schema_path, governor=None, min_gap_seconds=2.0) -> dict` returning `{scored, by_subscription, failed_over}`. Agreement = `round(1 - abs(frontier-cheap)/9.0, 3)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_frontier_pass.py
import sqlite3
from applypilot.fleet import frontier_pass as fp, frontier_db as fdb, cli_providers as clp

_DDL = """CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, full_description TEXT,
  fit_score INTEGER, research_fit_score REAL, audit_score REAL, duplicate_of_url TEXT, discovered_at TEXT);"""


def _brain():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.executescript(_DDL); fdb.ensure_frontier_schema(c)
    c.execute("INSERT INTO jobs (url, company, title, full_description, research_fit_score, fit_score, audit_score) "
              "VALUES ('u1','Acme','Chief of Staff','ops', 9.0, 9, 9)")
    c.commit(); return c


def test_subscription_path_writes_advisory_and_picks_top_model(monkeypatch, tmp_path):
    seen = {}
    def fake_codex(prompt, *, schema_path, model=None, **k):
        seen["model"] = model
        return {"score": 7, "reasoning": "frontier view"}
    monkeypatch.setattr(fp, "score_via_codex", fake_codex)
    c = _brain()
    res = fp.run_frontier_pass(c, resume_text="R", schema_path=str(tmp_path / "s.json"),
                               top_tier_floor=8.5, top_model="gpt-5.5", backlog_model="gpt-5.5-mini",
                               min_gap_seconds=0)
    assert res["scored"] == 1 and res["by_subscription"] == 1
    assert seen["model"] == "gpt-5.5"  # cheap_score 9.0 >= top_tier_floor -> top model
    row = c.execute("SELECT frontier_score, agreement, provider FROM frontier_scores WHERE url='u1'").fetchone()
    assert row["frontier_score"] == 7 and row["provider"] == "codex-subscription"
    assert abs(row["agreement"] - round(1 - abs(7 - 9) / 9.0, 3)) < 1e-6
    # ADVISORY ONLY: jobs canonical scores untouched
    j = c.execute("SELECT fit_score, audit_score, research_fit_score FROM jobs WHERE url='u1'").fetchone()
    assert j["fit_score"] == 9 and j["audit_score"] == 9 and j["research_fit_score"] == 9.0


def test_failover_to_metered_on_subscription_unavailable(monkeypatch, tmp_path):
    def boom(*a, **k): raise clp.SubscriptionUnavailable("limit")
    monkeypatch.setattr(fp, "score_via_codex", boom)
    monkeypatch.setattr(fp, "score_job", lambda *a, **k: {"score": 6, "reasoning": "api"})
    c = _brain()
    res = fp.run_frontier_pass(c, resume_text="R", schema_path=str(tmp_path / "s.json"),
                               metered_provider="gpt-5.5", min_gap_seconds=0)
    assert res["failed_over"] == 1 and res["by_subscription"] == 0
    row = c.execute("SELECT frontier_score, provider FROM frontier_scores WHERE url='u1'").fetchone()
    assert row["frontier_score"] == 6 and row["provider"] == "gpt-5.5"  # the metered model
```

- [ ] **Step 2: Run it, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# src/applypilot/fleet/frontier_pass.py
"""Home-side frontier pass: select the contested backlog, re-score each with the
Codex subscription (model by tier), fail over to a metered API on any limit, and
write advisory frontier_scores. Serial + governed. Never touches the jobs table."""
from __future__ import annotations

import random
import time

from applypilot.scoring.scorer import score_job, build_score_prompt_text
from applypilot.fleet import frontier_db
from applypilot.fleet.frontier_select import select_priority
from applypilot.fleet.cli_providers import score_via_codex, SubscriptionUnavailable
from applypilot.fleet.frontier_governor import FrontierGovernor


def _agreement(frontier, cheap):
    if frontier is None or cheap is None:
        return None
    return round(1 - abs(float(frontier) - float(cheap)) / 9.0, 3)


def run_frontier_pass(conn, *, resume_text, preference_profile=None, kg_prompt=None, limit=200,
                      floor=7.0, mode="backlog", hours=24, urls=None, use_subscription=True,
                      metered_provider="gpt-5.5", top_model="gpt-5.5", backlog_model="gpt-5.5",
                      top_tier_floor=8.5, schema_path=None, governor=None, min_gap_seconds=2.0) -> dict:
    jobs = select_priority(conn, limit=limit, floor=floor, mode=mode, hours=hours, urls=urls)
    gov = governor or FrontierGovernor("codex", min_gap_seconds=min_gap_seconds)
    scored = by_subscription = failed_over = 0
    for j in jobs:
        cheap = j.get("cheap_score")
        job = {"title": j["title"], "site": j.get("company"), "location": "N/A",
               "full_description": j.get("full_description")}
        model = top_model if (cheap is not None and cheap >= top_tier_floor) else backlog_model
        result, provider = None, None
        if use_subscription and gov.allow():
            try:
                prompt = build_score_prompt_text(resume_text, job, preference_profile, kg_prompt)
                result = score_via_codex(prompt, schema_path=schema_path, model=model)
                provider, by_subscription = "codex-subscription", by_subscription + 1
                gov.record("ok")
            except SubscriptionUnavailable:
                gov.record("limit")
                result = None
        if result is None:  # failover / subscription off / governor deny
            result = score_job(resume_text, job, preference_profile, kg_prompt, provider=metered_provider)
            provider, failed_over = metered_provider, failed_over + 1
        fscore = result.get("score")
        frontier_db.upsert_frontier_score(
            conn, url=j["url"], cheap_score=cheap, frontier_score=fscore, provider=provider,
            agreement=_agreement(fscore, cheap), reasoning=result.get("reasoning"),
        )
        scored += 1
        if min_gap_seconds:
            time.sleep(min_gap_seconds * (0.5 + random.random()))  # gap-jitter
    return {"scored": scored, "by_subscription": by_subscription, "failed_over": failed_over}
```

(Note: the tests pass `min_gap_seconds=0` so no real sleep; with the default the sleep is real — acceptable for the home-side pass. A test may monkeypatch `fp.time.sleep` if needed.)

- [ ] **Step 4: Run it, expect PASS** — `.conda-env/python.exe -m pytest tests/test_frontier_pass.py -q`. (If the jitter sleep slows tests, the tests already use `min_gap_seconds=0`.)

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/frontier_pass.py tests/test_frontier_pass.py
git commit -m "feat(fleet): frontier pass orchestrator (model-by-tier + failover, advisory)"
```

---

### Task 7: CLI `applypilot-fleet-frontier` + guardrail

**Files:**
- Create: `src/applypilot/fleet/frontier_main.py`
- Modify: `pyproject.toml` (add the console script under `[project.scripts]` — the table has `applypilot`, `applypilot-fleet-compute`, `applypilot-fleet-compute-home`; add one line, change nothing else)
- Test: `tests/test_frontier_main.py`

**Interfaces:**
- Produces: `make_schema_file(dir) -> path` (writes the score JSON Schema), `run_report(conn, max_agreement) -> list`, and `main(argv=None) -> int`. The subscription path requires `--enable-subscription`; without it, requesting `--provider codex-subscription` raises before any subprocess.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_frontier_main.py
import sqlite3
import pytest
from applypilot.fleet import frontier_main as fm, frontier_db as fdb

_DDL = "CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, full_description TEXT, fit_score INTEGER, research_fit_score REAL, duplicate_of_url TEXT, discovered_at TEXT);"


def test_subscription_requires_explicit_enable():
    with pytest.raises(SystemExit):
        fm.main(["--use-subscription", "--no-enable-subscription-guard-check"])  # see impl: codex-subscription w/o --enable-subscription
```

(Adjust the exact assertion to the implemented guard: the guard is "`use_subscription` true but `--enable-subscription` absent → `SystemExit`".)

- [ ] **Step 2: Run it, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# src/applypilot/fleet/frontier_main.py
"""applypilot-fleet-frontier: home-side frontier quality pass over the contested
backlog. Subscription backend is gated behind --enable-subscription (default off)."""
from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from applypilot import config
from applypilot.scoring import scorer
from applypilot.fleet import frontier_db
from applypilot.fleet.frontier_pass import run_frontier_pass

_SCORE_SCHEMA = {"type": "object", "properties": {"score": {"type": "integer"},
                 "reasoning": {"type": "string"}}, "required": ["score"]}


def make_schema_file(d) -> str:
    p = Path(d) / "score_schema.json"
    p.write_text(json.dumps(_SCORE_SCHEMA), encoding="utf-8")
    return str(p)


def _brain():
    c = sqlite3.connect(str(config.DB_PATH)); c.row_factory = sqlite3.Row
    return c


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="applypilot-fleet-frontier")
    p.add_argument("--mode", choices=["backlog", "new", "urls"], default="backlog")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--floor", type=float, default=7.0)
    p.add_argument("--top-model", default="gpt-5.5")
    p.add_argument("--backlog-model", default="gpt-5.5")
    p.add_argument("--top-tier-floor", type=float, default=8.5)
    p.add_argument("--metered-provider", default="gpt-5.5")
    p.add_argument("--use-subscription", action="store_true")
    p.add_argument("--enable-subscription", action="store_true")
    p.add_argument("--min-gap", type=float, default=2.0)
    p.add_argument("--report", action="store_true")
    args = p.parse_args(argv)

    if args.use_subscription and not args.enable_subscription:
        raise SystemExit("refusing the Codex subscription backend without --enable-subscription "
                         "(it runs `codex exec` on your logged-in account). Pass --enable-subscription to opt in.")
    conn = _brain()
    if args.report:
        for r in frontier_db.disagreement_report(conn):
            print(r)
        return 0
    ctx = scorer.load_score_context()
    with tempfile.TemporaryDirectory() as td:
        res = run_frontier_pass(
            conn, resume_text=ctx["resume_text"], preference_profile=ctx.get("preference_profile"),
            kg_prompt=ctx.get("kg_prompt"), limit=args.limit, floor=args.floor, mode=args.mode,
            use_subscription=args.use_subscription, metered_provider=args.metered_provider,
            top_model=args.top_model, backlog_model=args.backlog_model, top_tier_floor=args.top_tier_floor,
            schema_path=make_schema_file(td), min_gap_seconds=args.min_gap,
        )
    print(res)
    return 0
```

NOTE for the implementer: context comes from `scorer.load_score_context()` (Task 1) — no separate loader. For the guardrail test, you do NOT hit the brain (the `--enable-subscription` check raises before `_brain()`), so no DB monkeypatch is needed for that test; if you add a non-report happy-path test, monkeypatch `frontier_main.scorer.load_score_context` and `frontier_main.config.DB_PATH` (or `frontier_main._brain`) to a temp SQLite.

- [ ] **Step 4: Run it, expect PASS** (the guardrail test). Register `applypilot-fleet-frontier = "applypilot.fleet.frontier_main:main"` in `pyproject.toml`.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/frontier_main.py pyproject.toml tests/test_frontier_main.py
git commit -m "feat(fleet): applypilot-fleet-frontier CLI + subscription opt-in guard"
```

---

### Task 8: End-to-end + full fleet suite

**Files:**
- Test: `tests/test_frontier_e2e.py`

- [ ] **Step 1: Write the failing test** — full home-side pass with stubbed codex, asserting advisory-only + disagreement report:

```python
# tests/test_frontier_e2e.py
import sqlite3
from applypilot.fleet import frontier_pass as fp, frontier_db as fdb

_DDL = """CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, full_description TEXT,
  fit_score INTEGER, research_fit_score REAL, audit_score REAL, duplicate_of_url TEXT, discovered_at TEXT);"""


def test_frontier_pass_end_to_end_advisory_and_report(monkeypatch, tmp_path):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.executescript(_DDL); fdb.ensure_frontier_schema(c)
    c.executemany("INSERT INTO jobs (url, company, title, full_description, research_fit_score, fit_score, audit_score) VALUES (?,?,?,?,?,?,?)",
                  [("agree", "C", "COS", "d", 8.0, 8, 8), ("disagree", "C", "PM", "d", 9.0, 9, 9)])
    c.commit()
    scores = {"agree": 8, "disagree": 3}
    monkeypatch.setattr(fp, "score_via_codex",
                        lambda prompt, **k: {"score": scores["disagree"] if "PM" in prompt else scores["agree"], "reasoning": "x"})
    res = fp.run_frontier_pass(c, resume_text="R", schema_path=str(tmp_path / "s.json"), min_gap_seconds=0)
    assert res["scored"] == 2 and res["by_subscription"] == 2
    # advisory only
    assert c.execute("SELECT fit_score FROM jobs WHERE url='disagree'").fetchone()["fit_score"] == 9
    # the divergent one shows in the report; the agreeing one doesn't
    urls = [r["url"] for r in fdb.disagreement_report(c, max_agreement=0.8)]
    assert "disagree" in urls and "agree" not in urls
```

- [ ] **Step 2: Run it, expect FAIL/iterate** until green.

- [ ] **Step 3: Implement** — no new code; fix any wiring mismatch surfaced.

- [ ] **Step 4: Run the full fleet + frontier suite**

Run: `.conda-env/python.exe -m pytest tests/test_frontier_*.py tests/test_cli_providers.py tests/test_build_score_prompt_text.py tests/test_fleet_v3_*.py tests/test_fleet_compute_*.py -q`
Expected: all pass (the frontier tests + the prior 123 fleet tests; frontier tests need no Postgres, the fleet ones use the disposable PG).

- [ ] **Step 5: Commit & push**

```bash
git add tests/test_frontier_e2e.py
git commit -m "test(fleet): frontier pass end-to-end advisory + disagreement report"
git push private applypilot-hardening-and-brainstorm-integration
```

---

## Self-Review

**Spec coverage:** §3.1 selector → Task 3; §3.2 cli_providers → Task 4 (Claude cross-check deferred — see note); §3.3 governor → Task 5; §3.4 orchestrator (model-by-tier + failover) → Task 6; §3.5 frontier_scores → Task 2; §3.6 report + CLI → Tasks 2 & 7; §4 advisory-only/own-account/home-box/default-off → enforced in Tasks 6 (advisory), 4 (no token), 7 (`--enable-subscription` guard); §5 testing → every task + Task 8. **Deferred:** the optional Opus cross-check (`score_via_claude` + the pass branch) is NOT in these 8 tasks — it's a clean additive follow-up (a new `cli_providers.score_via_claude` + a `cross_check_opus` branch in `run_frontier_pass` + the `--cross-check-opus` flag), to add once the Codex path is proven. Flagged so it isn't silently dropped.

**Placeholder scan:** none remaining — the former `_load_context` gap is resolved by `scorer.load_score_context()` (Task 1). Every step has complete code.

**Type consistency:** `score_via_codex(...) -> dict` with a `score` key, consumed by `run_frontier_pass` (Task 6) matching its stub in Tasks 6/8; `FrontierGovernor.allow()/record()` (Task 5) used in Task 6; `upsert_frontier_score(...)` keyword args identical in Tasks 2 & 6; `select_priority(...)` shape consumed by Task 6; the agreement formula `round(1-abs(f-c)/9.0,3)` identical in Tasks 2-test, 6, 8.
