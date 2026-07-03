# Challenge-Triage Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Token-gate all console mutations and add a phone-friendly challenge-triage surface (GET /api/challenges + challenge ops + Challenges page + `challenges --grouped` CLI).

**Architecture:** Everything mutation-shaped stays behind the console's single POST route (`/api/action` → `run_action` op registry) — challenge actions become new ops, never new mutation primitives (they call `queue.resolve_challenge` / `queue.resolve_linkedin_challenge`-equivalent). The token check wraps `do_POST` before routing, fixing the audit's no-auth finding for ALL existing ops at once.

**Tech Stack:** Python 3.11 stdlib http server (`fleet/console_app.py`, single file, NO new deps), psycopg via `pgqueue.connect()`, pytest + the `fleet_db` disposable-PG fixture (tests/conftest.py).

**Spec:** `docs/superpowers/specs/2026-07-03-challenge-triage-console-design.md` (approved 2026-07-03).

## Global Constraints

1. Tests with `.\.conda-env\python.exe -m pytest` from `C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot`. Never `.venv`. PG tests use the `fleet_db` fixture (skip cleanly if the pgtest env is absent — never require live PG).
2. SHARED BRANCH: `git add` ONLY files you touched, never `-A`/`-u`. Don't touch launcher.py, diagnoser.py, or setup-fleet-pg-tailscale files.
3. HARD RULE for implementer subagents: do the work YOURSELF — do NOT spawn subagents/background tasks.
4. Console file conventions: stdlib only; no secrets/DSNs printed or logged; mutations idempotent; per-request `pgqueue.connect()` like the existing handlers.
5. Token compare uses `hmac.compare_digest`. Token value never logged; only the arm-URL is printed once at startup.
6. LinkedIn safety invariant: nothing here touches halt state. The lane's resolve function is the only linkedin mutation (queue.py:~615 — it already stamps auth_challenge internally; do NOT double-stamp).
7. Exact op names: `challenge_requeue`, `challenge_skip`, `challenge_skip_host`. Exact env var: `APPLYPILOT_CONSOLE_TOKEN`. Cookie name: `console_token`. Header: `X-Console-Token`. Group cap: 200 rows/request.

---

### Task 1: Console mutation token

**Files:**
- Modify: `src/applypilot/fleet/console_app.py` (`do_POST` at :973; `do_GET` at :925; module top for token helpers; `main` at :1513 prints the arm-URL)
- Test: `tests/test_console_token.py` (new)

**Interfaces:**
- Produces: module-level `get_console_token() -> str` — returns `os.environ["APPLYPILOT_CONSOLE_TOKEN"]` if set, else a process-cached `secrets.token_urlsafe(24)` generated once.
- Produces: `_token_ok(handler) -> bool` — reads `X-Console-Token` header, else `console_token` cookie (parse `Cookie` header via `http.cookies.SimpleCookie`); `hmac.compare_digest` against `get_console_token()`.
- `do_POST` FIRST action: `if not _token_ok(self): self._send_json(401, {"ok": False, "error": "bad token"}); return` — before any routing, so every existing op is gated too.
- `do_GET` for path `/` with `token=<t>` query param: if the param equals the token, respond 302 to `/` with `Set-Cookie: console_token=<t>; HttpOnly; Path=/`; if it doesn't match, ignore (no cookie) and render normally.
- `main` prints ONE line after the existing LAN-URL print: the arm URL with the token. Never print the token elsewhere.

**Steps (TDD):**
- [ ] Write failing tests in `tests/test_console_token.py`. Test the pure pieces without a live server: (1) `get_console_token` honors the env var (monkeypatch) and is stable across calls when generated; (2) `_token_ok` with a stub handler object (a `types.SimpleNamespace` with a `headers` dict-like) accepts the right header, right cookie, rejects wrong/missing; (3) POST gating: use the same in-process request pattern `tests/test_fleet_console_doctor.py` uses to exercise handlers — if it spins a real ThreadingHTTPServer on a free port, mirror that: POST `/api/action` without token → 401 and `run_action` NOT called (monkeypatch `run_action` to record calls); with header token → passes through to routing.
- [ ] Run: `.\.conda-env\python.exe -m pytest tests/test_console_token.py -q` → FAIL (helpers missing).
- [ ] Implement per the interfaces above.
- [ ] Re-run → PASS; also `.\.conda-env\python.exe -m pytest tests/test_fleet_console_doctor.py -q` — if its POSTs now 401, update THAT test's requests to send the token header (that is the intended behavior change; note it in your report).
- [ ] Commit: `git add src/applypilot/fleet/console_app.py tests/test_console_token.py tests/test_fleet_console_doctor.py && git commit -m "feat(console): token-gate all mutations (APPLYPILOT_CONSOLE_TOKEN)"`

---

### Task 2: GET /api/challenges

**Files:**
- Modify: `src/applypilot/fleet/console_app.py` (new GET route + `build_challenges()` builder near the other builders ~:406-536)
- Test: `tests/test_console_challenges_api.py` (new; `fleet_db` fixture)

**Interfaces:**
- Produces: `build_challenges() -> dict` opening its own `pgqueue.connect()`:
  ```sql
  -- open challenges
  SELECT url, kind, machine_owner, screenshot_url, raised_at
    FROM auth_challenge WHERE resolved_at IS NULL;
  -- parked rows, both lanes (lane literal added in Python)
  SELECT url, company, title, score, updated_at FROM apply_queue    WHERE apply_status='challenge_pending';
  SELECT url, company, title, score, updated_at FROM linkedin_queue WHERE apply_status='challenge_pending';
  ```
  Union by url (challenge fields NULL for parked-without-challenge rows; parked fields NULL for challenge-without-park). `host` = netloc of url (strip `www.`). Group kind (`(no challenge row)` for park-only) → host. Return `{"groups": [{"kind","host","rows":[{url,lane,company,title,score,kind,machine,screenshot_url,age_hours}]}], "counts": {"open_challenges": n, "parked": m}}`.
- `do_GET` route `path == "/api/challenges"` → `self._send_json(200, build_challenges())`, 503 JSON on DB error (mirror the existing status route's error handling).

**Steps (TDD):**
- [ ] Failing tests: seed `fleet_db` with one auth_challenge row + a matching parked apply_queue row, one parked linkedin_queue row with NO challenge row, one RESOLVED challenge (must not appear). Assert grouping, lanes, counts, and that the park-only row appears under `(no challenge row)`.
- [ ] Run → FAIL. Implement. Run → PASS.
- [ ] Commit: `git add src/applypilot/fleet/console_app.py tests/test_console_challenges_api.py && git commit -m "feat(console): /api/challenges — open challenges + parked rows, grouped"`

---

### Task 3: challenge ops in run_action

**Files:**
- Modify: `src/applypilot/fleet/console_app.py` (`run_action`'s op registry — find it via `grep -n "def run_action" src/applypilot/fleet/console_app.py`)
- Test: `tests/test_console_challenge_actions.py` (new; `fleet_db`)

**Interfaces:**
- Consumes: `queue.resolve_challenge(conn, url, requeue=True, commit=True)` (ats, queue.py:219) and the linkedin resolve at queue.py:~615 (find its exact name via grep; it ALREADY stamps auth_challenge — do not re-stamp for linkedin).
- Produces three ops in the registry:
  - `challenge_requeue` `{url, lane}` → lane-routed resolve with `requeue=True`; for the ats lane ONLY, additionally `UPDATE auth_challenge SET resolved_at=now(), outcome='solved' WHERE url=%s AND resolved_at IS NULL` IF the ats resolve does not already do so (READ queue.py:219-240 first; if it already stamps, don't).
  - `challenge_skip` `{url, lane}` → same with `requeue=False`, outcome `'skipped'`.
  - `challenge_skip_host` `{host, lane}` → select up to 200 parked urls on that host+lane, apply the skip path per url; message reports the count.
  - Unknown lane → `(False, "unknown lane")`; url not parked → success with `queue_rows: 0` semantics in the message (idempotent no-op). Return the existing `(ok, msg)` contract.

**Steps (TDD):**
- [ ] Failing tests: (1) requeue round-trip — park an apply_queue row (`status='leased', apply_status='challenge_pending'` + open auth_challenge row), run op, assert row `status='queued'`/`apply_status` cleared and challenge `resolved_at` set with outcome `solved`; run again → ok no-op. (2) skip → `challenge_skipped`/`blocked` per the lane's existing semantics + outcome `skipped`. (3) LANE ISOLATION: same url parked in BOTH queues; ats op must not touch the linkedin row (assert untouched) and vice versa. (4) skip_host caps at 200 and only hits that host. (5) LinkedIn halt state untouched: seed a rate_governor `account:linkedin` row before the linkedin op, assert byte-identical after.
- [ ] Run → FAIL. Implement. Run → PASS.
- [ ] Commit: `git add src/applypilot/fleet/console_app.py tests/test_console_challenge_actions.py && git commit -m "feat(console): challenge_requeue/skip/skip_host ops (resolve_challenge-only mutations)"`

---

### Task 4: Challenges page section

**Files:**
- Modify: `src/applypilot/fleet/console_app.py` (`_INDEX_HTML` — add a Challenges card/section + JS that fetches `/api/challenges`, renders groups, wires buttons to POST `/api/action` with `credentials: 'same-origin'` so the cookie flows)
- Test: `tests/test_console_challenges_page.py` (new; no PG needed for the smoke)

**Interfaces:**
- Section renders per-group header `kind · host · count` + `[Skip all on host]`, rows with `[Open job]` (href, target=_blank), `[I solved it → Re-queue]`, `[Skip]`. On 401 responses, show a fixed banner: `token expired — open the ?token= URL printed by the console window`. Match the existing dark-theme CSS variables; keep it one flexbox card consistent with the page.

**Steps:**
- [ ] Smoke tests: GET `/` HTML contains `id="challenges"` section and the fetch of `/api/challenges`; `?token=<right>` → 302 + Set-Cookie; `?token=<wrong>` → renders without Set-Cookie (reuse Task 1's server-spinning pattern).
- [ ] Implement HTML/JS. Run page + Task-1/2/3 suites → PASS.
- [ ] Commit: `git add src/applypilot/fleet/console_app.py tests/test_console_challenges_page.py && git commit -m "feat(console): challenges triage page (grouped, 2-tap disposition)"`

---

### Task 5: CLI `challenges --grouped` (both home mains)

**Files:**
- Modify: `src/applypilot/fleet/apply_home_main.py` (`challenges` subparser ~:68) and `src/applypilot/fleet/linkedin_home_main.py` (`challenges` subparser ~:145)
- Test: `tests/test_home_challenges_grouped.py` (new; `fleet_db`)

**Interfaces:**
- `--grouped` flag on the existing `challenges` subcommand of BOTH mains: prints a `kind × host → count` table from `auth_challenge WHERE resolved_at IS NULL` UNION the lane's parked rows (apply_home = apply_queue only; linkedin_home = linkedin_queue only). Reuse `build_challenges()`'s SQL shapes; do NOT import console_app (keep the CLI free of the http module) — put the shared query in `fleet/queue.py` as `challenge_summary(conn, lane) -> list[dict]` and have BOTH the console builder and the CLIs call it.

**Steps (TDD):**
- [ ] Failing test for `queue.challenge_summary` (seeded fleet_db: counts per kind×host×lane correct; resolved rows excluded).
- [ ] Implement `challenge_summary` + refactor Task 2's `build_challenges` to use it + wire `--grouped` in both mains (print via the existing plain-print style of those CLIs).
- [ ] Full console+queue sweep: `.\.conda-env\python.exe -m pytest tests/test_console_token.py tests/test_console_challenges_api.py tests/test_console_challenge_actions.py tests/test_console_challenges_page.py tests/test_home_challenges_grouped.py tests/test_fleet_console_doctor.py -q` → PASS.
- [ ] Commit: `git add src/applypilot/fleet/queue.py src/applypilot/fleet/console_app.py src/applypilot/fleet/apply_home_main.py src/applypilot/fleet/linkedin_home_main.py tests/test_home_challenges_grouped.py && git commit -m "feat(fleet): challenges --grouped (shared challenge_summary)"`

---

### Task 6: Final verification

- [ ] Full sweep: the Task-5 command PLUS `.\.conda-env\python.exe -m pytest tests -q -k "console or challenge"` — report counts.
- [ ] Read-only live sanity (mode-agnostic): `SELECT COUNT(*) FROM auth_challenge WHERE resolved_at IS NULL` + parked counts per lane via the conda python against the fleet PG — report the numbers the owner will see on first page load.
- [ ] Owner runbook line for the report: restart the console launcher (`run-fleet-console.ps1`), open the printed `?token=` URL once on the phone, triage from the Challenges card.
