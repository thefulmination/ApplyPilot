# Task C3 — Apply-Runtime Read-Only Triage

Lane: C3 (Claude), branch `claude/apply-runtime-triage`, worktree head `736235e`.
Runtime code base: `f9e998937da79d21bda05c6e4271873364d4c943` (no runtime code differs from
`f9e9989` at lane creation; the lane head adds only the docs-only coordination commit).

**This lane is diagnosis only.** No source, test, workflow, fleet state, secret, or deployment
file was modified. The only file written is this report. Nothing was deployed, unpaused, or
issued to the fleet, and production PostgreSQL was never touched — every Postgres-backed test
below ran against the disposable ephemeral instance stood up by the `fleet_db` fixture
(`postgresql://postgres:applypilot-disposable-test-only@127.0.0.1:<random-port>/postgres`).

Interpreter: bare `python` on PATH =
`C:\Users\JStal\AppData\Local\Programs\Python\Python312\python.exe` (3.12.10, pytest 9.1.1).
All runs serial. No `-n` / xdist (xdist has known isolation problems in this repo, and the plan
wants serial results).

---

## Step 1 — Reproduce each cluster serially

Each command was run separately, from the worktree root, exactly as specified in the brief.

| # | Command | Result |
|---|---|---|
| 1 | `python -m pytest -q tests/test_apply_agent_selection.py` | **4 failed, 17 passed** in 3.90s |
| 2 | `python -m pytest -q tests/test_apply_channel.py` | **7 failed, 14 passed** in 2.01s |
| 3 | `python -m pytest -q tests/test_console_challenge_actions.py` | **5 failed, 4 passed** in 28.62s |
| 4 | `python -m pytest -q tests/test_browser_readiness_preflight.py` | **1 failed, 3 passed** in 11.22s |
| 5 | `python -m pytest -q tests/test_fleet_apply_home.py tests/test_fleet_apply_lane.py` | **3 failed, 27 passed** in 34.17s |
| 6 | `python -m pytest -q tests/test_gmail_reauth.py tests/test_greenhouse_submit.py` | **1 failed, 46 passed** in 3.56s |
| 7 | `python -m pytest -q tests/test_inbox_auth_mail_source.py tests/test_inbox_auth_redaction.py` | **2 failed, 42 passed** in 14.85s |

**Total across this lane's clusters: 23 failed, 153 passed, 0 errors.**

`tests/test_gmail_reauth.py` contributes **0 failures** — it is fully green; every failure in
command 6 comes from `tests/test_greenhouse_submit.py`. That is a real and useful result: the
Gmail re-auth surface needs no repair.

### Exact failing test node IDs (23)

```
tests/test_apply_agent_selection.py::test_greenhouse_shadow_result_records_adapter_route_stats
tests/test_apply_agent_selection.py::test_greenhouse_owned_submit_uses_attempt_store_and_records_verifier
tests/test_apply_agent_selection.py::test_greenhouse_runtime_required_fields_park_before_submit_without_agent
tests/test_apply_agent_selection.py::test_greenhouse_submit_mode_parks_incomplete_plan_without_agent_fallback

tests/test_apply_channel.py::test_make_apply_fn_retries_auth_gated_browser_failure_with_prearmed_otp
tests/test_apply_channel.py::test_make_apply_fn_records_magic_link_kind_without_secret
tests/test_apply_channel.py::test_auth_required_reuses_prearmed_request_for_full_bounded_wait
tests/test_apply_channel.py::test_auth_required_assisted_retry_runs_at_most_once
tests/test_apply_channel.py::test_prearmed_auth_without_hint_reports_no_assisted_retry
tests/test_apply_channel.py::test_assisted_retry_terminal_includes_bounded_terminal_failure
tests/test_apply_channel.py::test_controlled_apply_successful_cleanup_is_true_and_retry_stays_bounded

tests/test_console_challenge_actions.py::test_challenge_requeue_apply_round_trip_then_idempotent
tests/test_console_challenge_actions.py::test_challenge_skip_apply_sets_blocked_and_skipped_outcome
tests/test_console_challenge_actions.py::test_challenge_op_never_crosses_lanes_for_same_url
tests/test_console_challenge_actions.py::test_challenge_skip_host_scopes_to_host_and_lane
tests/test_console_challenge_actions.py::test_challenge_skip_host_caps_at_200

tests/test_browser_readiness_preflight.py::test_worker_requeues_untouched_browser_preflight_failure

tests/test_fleet_apply_home.py::test_apply_home_resolve_challenge
tests/test_fleet_apply_lane.py::test_lease_rejects_prior_browser_or_requeue_evidence
tests/test_fleet_apply_lane.py::test_canary_caps_total_leases_fleetwide

tests/test_greenhouse_submit.py::test_launcher_does_not_release_agent_fallback_when_page_close_fails_after_submit

tests/test_inbox_auth_mail_source.py::test_answer_pending_defaults_to_mail_source
tests/test_inbox_auth_redaction.py::test_run_job_redacts_magic_link_from_every_persistent_sink_and_parses_result
```

### Relationship to the C1 lane's finding

The sibling C1 lane traced many failures elsewhere to the deliberate fail-closed guard at
`src/applypilot/apply/pgqueue.py:161` (`RuntimeError: Fleet Postgres DSN must contain an
explicit password`). **None of the 23 failures in this lane share that root cause.** It was
checked per-cluster, not assumed: that guard raises at `pgqueue.connect()`, and every cluster
here either connects successfully to the disposable fixture DSN (which carries an explicit
password) or never calls `connect()` at all. No traceback in any of the seven runs mentions
`pgqueue.py:161` or that message. C1's fix will not move any number in this lane.

---

## Step 2 — Root-cause groups

Seven groups. All 23 failures are accounted for; no failure is unexplained.

**Unifying theme:** every group is downstream of one architectural migration — apply/fleet
mutations moving out of Python-side SQL and into `SECURITY DEFINER` Postgres functions gated by
the `fleet_worker_lease_ledger` and a per-session lease identity. That migration is mostly
correct and is the authority model the cutover wants. Five groups are stale test doubles that
were never updated to the new contract. **Two groups are genuine production regressions where a
safety property was silently dropped during the migration** — those are the ones that matter
for release.

---

### G1 — Browser interaction must be bound to the active lease (6 tests)

**Failing:** all 4 in `test_apply_agent_selection.py`, plus
`test_greenhouse_submit.py::test_launcher_does_not_release_agent_fallback_when_page_close_fails_after_submit`,
plus `test_inbox_auth_redaction.py::test_run_job_redacts_magic_link_from_every_persistent_sink_and_parses_result`.

**First bad call / state transition.** Two sites in `src/applypilot/apply/launcher.py`:

1. `src/applypilot/apply/launcher.py:2117-2119`, inside `_maybe_greenhouse_apply()`:
   ```python
   if attempt_store is None or not hasattr(attempt_store, "mark_browser_interaction"):
       raise RuntimeError("Greenhouse browser path lacks a lease interaction marker")
   attempt_store.mark_browser_interaction()
   ```
   The tests pass `types.SimpleNamespace()` or a hand-rolled `AttemptStore` exposing only
   `create_prepared`/`transition` — no `mark_browser_interaction`. The guard raises. The raise
   is caught by the broad `except Exception as exc:` at `launcher.py:2127`; because `attempt_id` is
   still `None` (the failure happens *before* `on_plan_ready` runs), control reaches
   `launcher.py:2174-2175` — `logger.debug("greenhouse adapter failed before submit; agent
   proceeds"); return None`. The test then does `result[0]` and gets
   `TypeError: 'NoneType' object is not subscriptable`, or pops `_adapter_route_stats` and gets
   `KeyError: 91`.

2. `src/applypilot/apply/launcher.py:2760`, inside `_run_job_impl()`:
   ```python
   interaction_marker=getattr(attempt_store, "mark_browser_interaction", None),
   ```
   With no store, `interaction_marker` is `None`, so
   `_ExecutionEvidence.prepare_tool()` (`launcher.py:1935-1937`) raises
   `RuntimeError("browser interaction boundary is not bound to the active lease")` on the first
   interaction tool. Observed: `assert 'failed:browser interaction boundary is not bound to the
   active lease' == 'applied'`.

**Likely owning production function.** `launcher._maybe_greenhouse_apply()` and
`launcher._run_job_impl()` / `launcher._ExecutionEvidence.prepare_tool()`. The marker itself is
`FleetAttemptStore.mark_browser_interaction()` at
`src/applypilot/fleet/apply_worker_main.py:256-258`, which calls the SQL function
`public.fleet_worker_mark_browser_interaction()` (declared in
`src/applypilot/fleet/pg_roles.py:105`).

**Test or production stale? — TEST IS STALE. Production is correct and deliberate.**

Evidence, not inference:
- `tests/test_apply_execution_evidence.py:36-39` is a *currently passing* test that explicitly
  asserts this exact fail-closed behavior:
  `with pytest.raises(RuntimeError, match="not bound to the active lease")`. The invariant is
  specified, intended, and covered.
- `tests/test_apply_usage_limit_requeue.py:161, 218, 280` show the *updated* fake pattern —
  those doubles each define `def mark_browser_interaction(self): return None` and their tests
  pass. The six failing tests are simply doubles from the same family that were not updated in
  the same sweep.

This is a fail-closed lease-authority guard. Per the shared baseline it must **not** be weakened
to make an old test pass.

**One nuance Codex should decide explicitly, not by default.** The guard at `launcher.py:2117`
fires in **shadow** mode too (`test_greenhouse_shadow_result_records_adapter_route_stats` calls
`_maybe_greenhouse_apply` with the default `attempt_store=None` and `submit_enabled -> False`).
Shadow mode has no attempt ledger by design, yet it *does* drive a real page via
`page.goto(...)` on the live CDP browser at `launcher.py:2120`. So requiring the marker in
shadow is *consistent* with the invariant "no browser interaction without a bound lease", and I
judge the guard correct as written. The consequence is that shadow validation is now unusable
without a store — which is the intended tightening, not a bug, but it is a behavior change the
owner should be told about rather than discover in the field.

**Smallest safe repair (test-side only).** Give each failing double a
`mark_browser_interaction()` no-op, matching `tests/test_apply_usage_limit_requeue.py:161`. For
the shadow test, pass a minimal store exposing only that method. Do not touch
`launcher.py:2117` or `:2760`.

**Codex lane: X1** (`launcher.py` + `greenhouse_submit.py` + directly associated tests).

---

### G2 — `_prearm_inbox_auth_request` gained a `conn` keyword (7 tests)

**Failing:** all 7 in `tests/test_apply_channel.py`.

**First bad call.** `src/applypilot/fleet/apply_worker_main.py:365-368`:
```python
prearmed_request_id = (
    launcher._prearm_inbox_auth_request(
        job, conn=attempt_store.conn if attempt_store is not None else None
    )
    ...
```
Every failing test monkeypatches `launcher._prearm_inbox_auth_request` with a `lambda job: ...`
or a `def fake_prearm(job)` that accepts no `conn`. Uniform failure — verified by collecting
every `E ` line across the run; all 7 are
`TypeError: ...<lambda>() got an unexpected keyword argument 'conn'`.

**Likely owning production function.**
`launcher._prearm_inbox_auth_request()` at `src/applypilot/apply/launcher.py:3312`:
`def _prearm_inbox_auth_request(job: dict, *, conn=None) -> int | None:`.

**Test or production stale? — TEST IS STALE. Production is correct.**

Evidence: `git log -L 3312,3316:src/applypilot/apply/launcher.py` shows commit `d486d4a`
("wip: snapshot canonical brain phase one") deliberately widening the signature:
```
-def _prearm_inbox_auth_request(job: dict) -> int | None:
+def _prearm_inbox_auth_request(job: dict, *, conn=None) -> int | None:
```
The body (`launcher.py:3316-3338`) uses it correctly: `owned = conn is None`; when a connection
is injected it reuses it and does **not** close it (`finally: if owned: conn.close()`). Passing
the worker's existing pooled connection instead of opening a second one against
`FLEET_PG_DSN` is a real improvement — it keeps the OTP relay request inside the worker's
session, which the lease-identity model depends on. The production side is right; only the
test doubles were left behind.

**Smallest safe repair (test-side only).** Change the 7 doubles to accept `**_kwargs` (or an
explicit `conn=None`). No production change.

**Codex lane: see the overlap note below — this is the one to route carefully.**

---

### G3 — `park_challenge` now requires a real ledger lease + session lease identity (6 tests)

**Failing:** all 5 in `test_console_challenge_actions.py`, plus
`test_fleet_apply_home.py::test_apply_home_resolve_challenge`.

**First bad state transition.** `src/applypilot/fleet/queue.py:768-786` (`park_challenge`) now
delegates entirely to SQL:
```python
cur.execute("SELECT public.fleet_worker_park('ats',%s,%s,%s,0,%s) AS ok", ...)
```
`public.fleet_worker_park` (`src/applypilot/fleet/schema_v3.sql:1825-1909`) returns FALSE at two
gates before doing any work:
- `schema_v3.sql:1842-1846`: `SELECT * INTO lease FROM public.fleet_worker_lease_ledger l WHERE
  l.lane=p_lane AND l.url=p_url AND l.worker_id=p_worker AND l.state='leased' ... IF NOT FOUND
  THEN RETURN FALSE; END IF;`
- `schema_v3.sql:1847-1849`: the session GUC `applypilot.worker_lease_id` must equal
  `lease.lease_id`, else `RETURN FALSE`.

Both failing test families fake a lease with a raw `UPDATE apply_queue SET status='leased',
lease_owner=...` (e.g. `tests/test_console_challenge_actions.py:80-87`,
`tests/test_fleet_apply_home.py:~365`). That writes no `fleet_worker_lease_ledger` row and sets
no GUC, so `fleet_worker_park` correctly returns FALSE. Observed:
`assert False is True` (5x) and, for `test_apply_home_resolve_challenge`, the knock-on
`assert 'leased' == 'queued'` — `park_challenge` silently no-oped, so `apply_status` was never
`challenge_pending`, so `resolve_challenge` (`queue.py:789-808`, which filters on
`apply_status='challenge_pending'`) matched zero rows.

**Likely owning production function.** `queue.park_challenge()` →
`public.fleet_worker_park` (SQL), with `queue.resolve_challenge()` /
`apply_home_main.resolve_challenge_cmd()` as the downstream consumer.

**Test or production stale? — TEST IS STALE. Production is correct.**

Evidence: the GUC is set by the real lease path — `fleet_worker_lease_ats` ends with
`PERFORM pg_catalog.set_config('applypilot.worker_lease_id', new_lease::text, FALSE)`
(`schema_v3.sql:1428`) after inserting the `fleet_worker_lease_ledger` row
(`schema_v3.sql:1412-1422`). So a genuinely leased row *does* satisfy both gates. Only a
hand-forged `UPDATE` fails, which is exactly what the ledger authority exists to prevent — a
row that looks leased in `apply_queue` but has no provable lease behind it. Making
`fleet_worker_park` accept that would reintroduce the hole.

**Smallest safe repair (test-side only).** Replace the raw-SQL fake lease with a real
`queue.lease_apply(...)` (which produces the ledger row and sets the GUC), or add a shared test
helper that inserts the `fleet_worker_lease_ledger` row and `set_config`s
`applypilot.worker_lease_id` to match. Prefer the former — it exercises the real contract. Do
not relax `fleet_worker_park`.

**Codex lane: X2.**

---

### G4 — PRODUCTION REGRESSION: `lease_apply` silently dropped the double-apply blocker (1 test)

**Failing:** `tests/test_fleet_apply_lane.py::test_lease_rejects_prior_browser_or_requeue_evidence`.
Its docstring: "Review-only canary blockers must also hold at the real lease boundary."

**First bad state transition.** `src/applypilot/fleet/queue.py:292-303` — `lease_apply()` now
executes *only* `SELECT * FROM public.fleet_worker_lease_ats(...)`. The candidate-selection
`WHERE` clause inside `fleet_worker_lease_ats` (`schema_v3.sql:1330-1378`) **does not contain**
the two blockers that the pre-migration Python SQL had.

The pre-migration query `_LEASE_APPLY` (`src/applypilot/fleet/queue.py:173-289`) still exists in
the file and still has them, at `queue.py:195-206`:
```sql
AND COALESCE(q.apply_error, '') NOT ILIKE 'requeued_by_%%'
AND NOT EXISTS (
  SELECT 1 FROM apply_result_events prior
  WHERE prior.queue_name = 'apply_queue' AND prior.url = q.url
    AND ( COALESCE(prior.application_tool_calls, 0) > 0
       OR COALESCE(prior.apply_error, '') ILIKE 'requeued_by_%%' )
)
```
**`_LEASE_APPLY` is now dead code.** `grep -rn "_LEASE_APPLY" src/ tests/` returns only its own
definition at `queue.py:173` and a comment at `queue.py:1060`. Nothing executes it. That is the
tell: the clause was not deliberately removed and replaced — it was left behind in an orphaned
string when the lease moved into the SQL function.

Observed: `assert {'url': 'u0', ...} is None` — the queue leased `u0`, a row whose prior
`apply_result_events` record has `application_tool_calls=3`, i.e. **a prior attempt that
provably touched the form**. The sibling row `u1` carries
`apply_error='requeued_by_remediator:usage_limit'` and is likewise no longer filtered.

**Likely owning production function.** `public.fleet_worker_lease_ats` — specifically the
candidate `SELECT ... WHERE` at `src/applypilot/fleet/schema_v3.sql:1330-1378`, whose existing
`NOT EXISTS` guards (`apply_attempts` in `submit_started`/`submitted_unverified` at
`schema_v3.sql:1344-1349`, and `applied_set` at `schema_v3.sql:1350`) are strictly narrower than
what was lost.

**Test or production stale? — PRODUCTION IS WRONG. The test is the correct spec.**

I commit to this without hedging. Reasoning: the surviving guards only catch a prior attempt
that reached `submit_started`/`submitted_unverified` or landed in `applied_set`. The dropped
guard was broader by design — it caught *any* prior event with `application_tool_calls > 0`,
i.e. an attempt that drove the browser but crashed before the ledger could record a submit
state. That is precisely the `crash_unconfirmed` class this project has repeatedly identified
as the double-apply hazard: an attempt that may have submitted but cannot prove it. Re-leasing
those rows re-drives the form on a second machine. This is a release-blocking safety
regression, and it is squarely inside the "do not weaken double-apply protection" fence of the
shared baseline. The remediator's `requeued_by_remediator:*` tag
(`src/applypilot/fleet/remediator.py:12`) is the same story: a row the remediator touched must
not be blind-re-leased.

**Smallest safe repair.** Port the two clauses from `queue.py:195-206` into the
`fleet_worker_lease_ats` candidate `SELECT` in `schema_v3.sql` (they are pure `WHERE`
predicates; no restructuring needed), then delete the dead `_LEASE_APPLY` string so the next
migration cannot mistake it for live. Fail-closed direction — it can only reduce leases, never
increase them.

**Codex lane: X2.**

---

### G5 — Canary test defeated by the new lease-time governor stamp (1 test)

**Failing:** `tests/test_fleet_apply_lane.py::test_canary_caps_total_leases_fleetwide`.
Observed: `a is not None and b is None` — only **1** of an expected 2 leases succeeded.

**First bad state transition.** `fleet_worker_lease_ats` now writes the rate governor at *lease*
time (`src/applypilot/fleet/schema_v3.sql:1395-1399`):
```sql
UPDATE public.rate_governor SET count_24h=count_24h+1,
  last_attempt_at=pg_catalog.now(),updated_at=pg_catalog.now()
WHERE scope_key IN ('global','home_ip:'||p_home_ip,'host:'||COALESCE(job.target_host,job.apply_domain)) ...
```
The candidate `SELECT` then enforces the host min-gap against that same column
(`schema_v3.sql:1374-1378`): `COALESCE(host.last_applied_at, host.last_attempt_at) < now() -
make_interval(secs => GREATEST(COALESCE(host.min_gap_seconds,90), ...))`.

All 5 rows the test seeds share `apply_domain='acme.com'`
(`tests/test_fleet_apply_lane.py:36`), so they share the governor scope `host:acme.com`, whose
`min_gap_seconds` defaults to **90** (`schema_v3.sql:414`). The governor row is auto-created by
`queue._sync_worker_lease_authority()` (`queue.py:115-118`). Lease #1 stamps
`last_attempt_at=now()`; leases #2 and #3 are then blocked by the 90-second host gap and never
reach the canary counter. The test's `canary_remaining=2` expectation is never exercised.

**Why this used to pass.** The pre-migration `_LEASE_APPLY` (`queue.py:173-289`) **never touched
`rate_governor` at all** — no `count_24h` increment, no `last_attempt_at` stamp. Its own inline
comment at `queue.py:254-256` states the intent: "last_attempt_at is now stamped on every
*outcome* (success+captcha+block)". Stamping at *lease* time is new behavior introduced by the
migration.

**Test or production stale? — TEST IS STALE (in its setup); production is defensible.**

The property the test asserts — "a canary of 2 permits exactly 2 fleetwide leases" — is still
correct and worth keeping. What is stale is the *fixture*: three immediate leases against a
single host is no longer a valid way to reach the canary gate, because an unrelated
account-safety control (host min-gap) now fires first.

I judge the lease-time stamp defensible rather than wrong: stamping on lease is *stricter* than
stamping on outcome, and it closes a real window where several workers could lease the same host
back-to-back before any outcome landed. That matches this project's standing account-safety
posture (per-host gap/jitter). **But I flag one thing for Codex that I could not settle from the
code alone:** the new SQL dropped the jitter multiplier `* (0.7 + random()*0.7)` that
`_LEASE_APPLY` applied to the gap (`queue.py:260`). The gap is now a fixed 90s rather than a
jittered 63-153s. That makes inter-request spacing deterministic and therefore more
fingerprintable — a small but real regression against the gap-jitter intent. It is not what
fails this test, and I am not certain whether it was dropped deliberately (a `SECURITY DEFINER`
function could reasonably avoid `random()` for reproducibility) or by oversight. **What would
settle it:** the commit message / review notes for the change that introduced
`fleet_worker_lease_ats`, or an owner ruling on whether jitter is required at the lease
boundary. Worth a decision, not a silent carry-forward.

**Smallest safe repair (test-side only).** Seed the rows across distinct
`apply_domain`/`target_host` values so each lease uses its own governor scope, or set
`min_gap_seconds=0` on `host:acme.com` in the test setup. Either keeps the canary assertion
intact and removes the unrelated coupling. Do not remove the min-gap from production.

**Codex lane: X2.**

---

### G6 — PRODUCTION REGRESSION: infrastructure park lost the attempt refund and the failure counter (1 test)

**Failing:** `tests/test_browser_readiness_preflight.py::test_worker_requeues_untouched_browser_preflight_failure`.
Observed: `assert 1 == 0` on `row["attempts"]`.

**First bad state transition.** `src/applypilot/fleet/queue.py:493-505`
(`park_infrastructure_failure`) now routes through `_terminalize(...)` →
`public.fleet_worker_terminalize`, which is the *terminal* write path and does **not** decrement
`attempts`, does **not** increment `infrastructure_failure_count`, and does **not** set
`infrastructure_last_failure_at`. The lease itself incremented `attempts` at
`schema_v3.sql:1404-1409` (`attempts=q.attempts+1`), so the row is left with `attempts=1`.

**Direct evidence this is a regression, not a redefinition.** `git show d486d4a --
src/applypilot/fleet/queue.py` shows the previous implementation being deleted wholesale:
```
-            "UPDATE apply_queue SET status='failed'::apply_queue_status, "
-            "apply_status='infrastructure_pending', apply_error=%s, "
-            "attempts=GREATEST(attempts-1,0), infrastructure_failure_count="
-            "COALESCE(infrastructure_failure_count,0)+1, infrastructure_last_failure_at=now(), "
-            "lease_owner=NULL, lease_expires_at=NULL, updated_at=now() "
...
+    landed = _terminalize(conn, "ats", worker_id, url, status="failed",
+        apply_status="infrastructure_pending", ...)
```
Three behaviors (`attempts` refund, `infrastructure_failure_count` increment,
`infrastructure_last_failure_at`) were dropped in the same edit that swapped in `_terminalize`.
Nothing replaced them: `grep -rn "infrastructure_pending\|infrastructure_preflight_failure"` over
`worker.py`/`queue.py`/`schema_v3.sql` finds no other writer. The sibling
`fleet_worker_requeue` path *does* still refund (`schema_v3.sql:1655-1657`,
`attempts=GREATEST(attempts-1,0)`), which shows the refund concept survived elsewhere — it was
just lost on this specific branch.

**Test or production stale? — PRODUCTION IS WRONG. The test is the correct spec.**

The invariant is sound and worth defending: a browser preflight failure means the local Chrome
never came up (`worker.py:759` gates on `infrastructure_preflight_failure and not
res.get("application_tool_calls")` — i.e. the job was provably *never touched*). Burning a retry
attempt for a local infrastructure fault penalizes the *job* for a fault of the *machine*, and
over repeated preflight failures it walks good jobs into their attempt ceiling and out of the
queue permanently. The lost `infrastructure_failure_count` is worse in a subtler way: there is a
partial index built specifically on it (`schema_v3.sql:61-63`,
`idx_apply_queue_infrastructure_pending`), so an operator surface exists that is now reading a
column nothing increments — it will silently report zero infrastructure failures forever.

**Smallest safe repair.** Restore the three writes on the infrastructure-park branch. Cleanest
placement is inside the `p_lane='ats'` terminal branch of `fleet_worker_terminalize`, conditional
on `p_apply_status='infrastructure_pending'` (so the ordinary terminal path is untouched);
alternatively re-add a dedicated guarded `UPDATE` in `park_infrastructure_failure` after
`_terminalize` returns True. Prefer the former — it keeps the mutation inside the lease-guarded
`SECURITY DEFINER` boundary rather than reopening a direct Python-side write, which is the whole
point of the migration.

**One more thing Codex must expect on this test.** After the `attempts`/counter fix, the test
still asserts `assert row["infrastructure_failure_count"] == 1`
(`tests/test_browser_readiness_preflight.py:133`) and `assert events == 0`
(`:134`, i.e. no `apply_result_events` row with `status <> 'leased'`). `_terminalize` →
`fleet_worker_terminalize` is an event-emitting path, so the `events == 0` assertion may fail
next. **I did not verify that**, because verifying it requires applying the fix, which is
outside this read-only lane. Flagging it so Codex is not surprised by a second red on the same
test. If the event *is* emitted, the judgment call is whether an untouched infrastructure park
should be a recorded apply outcome at all — my read of `events == 0` is that it should not,
since nothing was attempted against the employer.

**Codex lane: X2.**

---

### G7 — `otp_relay` pending query moved into a controller SQL function (1 test)

**Failing:** `tests/test_inbox_auth_mail_source.py::test_answer_pending_defaults_to_mail_source`.

**First bad call.** `src/applypilot/fleet/otp_relay.py:245` → `_answer_pending_locked`, which now
opens with:
```python
cur.execute("SELECT public.fleet_controller_otp_pending(%s) AS pending", (_MAX_RESPONDER_ITEMS + 1,))
pending = cur.fetchone()["pending"] or []
```
The test's `_FakeCursor.execute` (`tests/test_inbox_auth_mail_source.py:~440`) only recognizes
`pg_try_advisory_lock` / `pg_advisory_unlock` and otherwise leaves `self._row = None`; the old
code read rows via `fetchall()`. Observed: `TypeError: 'NoneType' object is not subscriptable`.

**Likely owning production function.** `otp_relay._answer_pending_locked()`, reading
`public.fleet_controller_otp_pending`.

**Test or production stale? — TEST IS STALE. Production is correct.**

Same migration as G3/G4: the pending-request read moved behind a controller-owned `SECURITY
DEFINER` function so the responder cannot select arbitrary relay rows. The test's hand-rolled
cursor double simply does not implement the new call. Note the failure is in the double's
plumbing, *before* the assertion the test actually cares about (that `gmail_service=None`
resolves via `get_mail_source()` and forwards fetched messages into
`scan_gmail_for_auth_codes(messages=...)`). That behavior is very likely still intact — the test
never reaches it.

**Smallest safe repair (test-side only).** Teach `_FakeCursor.execute` to answer
`fleet_controller_otp_pending` with `{"pending": [pending_row]}` via `fetchone()`. No production
change.

**Codex lane: X2** (`src/applypilot/fleet/otp_relay.py` is a fleet file). See the overlap note.

---

## Shared-file overlap — READ THIS BEFORE ASSIGNING X1 AND X2

**`src/applypilot/apply/launcher.py` is touched by two groups, but needs NO production edit.**

- **G1** implicates `launcher.py:2117-2119` and `launcher.py:2760`.
- **G2** implicates `launcher.py:3312` (the `conn=` signature).

**Both are TEST-side fixes.** Under this triage, `launcher.py` itself requires **zero source
changes**. That is the single most useful de-confliction fact in this report: X1's `launcher.py`
ownership is not actually contended by anything X2 must do, provided X2 does not "helpfully"
revert the `conn=` keyword at `launcher.py:3312` to make `tests/test_apply_channel.py` pass. It
must not — the caller at `src/applypilot/fleet/apply_worker_main.py:365-368` (an X2 file) depends
on it. **A narrowing of `launcher._prearm_inbox_auth_request` would break an X2 file, and a
change to `apply_worker_main.py:366` would break an X1 file.** Neither is correct; the correct
fix is entirely inside `tests/test_apply_channel.py`.

**Recommended routing for the one genuinely ambiguous test file:**
`tests/test_apply_channel.py` (G2) exercises `fleet/apply_worker_main.py` *through* `launcher`,
so it reads as "directly associated" to both lanes. **Assign it to X1**, because the stale
symbol is a `launcher` symbol and X1 owns `launcher.py` — X1 has the context to confirm the
signature is intentional. X2 should not open `tests/test_apply_channel.py`.

**No other file is contended.** G3/G4/G5/G6/G7 are confined to
`src/applypilot/fleet/queue.py`, `src/applypilot/fleet/schema_v3.sql`,
`src/applypilot/fleet/otp_relay.py` and their tests — all X2. G1's production sites are
`launcher.py` and `greenhouse_submit.py` — both X1, and both read-only for this remediation.

---

## Dependency order

Which groups must be fixed before others, and why.

**Tier 0 — fix first; it changes several clusters at once.**
- **G4** (`fleet_worker_lease_ats` blockers). This is the shared helper every Postgres-backed
  apply test leases through. Restoring the two `WHERE` predicates changes which rows are
  leasable fleetwide, so it can move results in G3, G5, and G6 (all of which lease before they
  assert). Fixing G4 *after* those means re-running and possibly re-fixing them. Do G4 first,
  then re-run clusters 3, 4, and 5 before touching anything else in X2.

**Tier 1 — independent of each other; safe in parallel once Tier 0 lands.**
- **G6** (infrastructure park refund) — `fleet_worker_terminalize` / `park_infrastructure_failure`.
  Independent of G4's predicates, but its test leases first, so sequence it after G4.
- **G3** (test-side ledger lease helper). If the fix takes the recommended form — call the real
  `queue.lease_apply()` instead of forging a lease — then G3 **depends on G4**, because the rows
  it leases must actually be leasable under the restored predicates. This is the one real
  cross-group coupling and the reason G4 leads.
- **G5** (test-side host seeding).

**Tier 2 — fully independent; may be done at any time, by either lane, in parallel with everything above.**
- **G1** (test doubles gain `mark_browser_interaction`) — no Postgres, no shared helper.
- **G2** (test doubles accept `conn`) — no Postgres, no shared helper.
- **G7** (test cursor double learns `fleet_controller_otp_pending`) — no shared helper.

**Net:** the critical path is **G4 → (G3, G6) → re-run**. Everything else is embarrassingly
parallel. If X1 and X2 run concurrently, X1's entire scope (G1 + G2) is Tier 2 and cannot
collide with X2's critical path.

---

## Recommended Codex lane ownership

| Group | Failing | Root cause | Verdict | Production change needed? | Lane |
|---|---|---|---|---|---|
| G1 | 6 | Browser interaction not lease-bound (`launcher.py:2117`, `:2760`) | Test stale | **No** | **X1** |
| G2 | 7 | `_prearm_inbox_auth_request(conn=)` (`launcher.py:3312`) | Test stale | **No** | **X1** |
| G3 | 6 | `fleet_worker_park` needs ledger lease + GUC (`schema_v3.sql:1842-1849`) | Test stale | **No** | **X2** |
| G4 | 1 | `fleet_worker_lease_ats` dropped double-apply blocker (`schema_v3.sql:1330-1378`) | **Production wrong** | **Yes** | **X2** |
| G5 | 1 | Lease-time governor stamp vs host min-gap (`schema_v3.sql:1395-1399`) | Test stale (setup) | **No** | **X2** |
| G6 | 1 | Infra park lost attempt refund + counter (`queue.py:493-505`) | **Production wrong** | **Yes** | **X2** |
| G7 | 1 | `fleet_controller_otp_pending` (`otp_relay.py:245`) | Test stale | **No** | **X2** |

**X1 total: 13 failures, zero production changes** — all thirteen are stale test doubles that
never learned the lease-marker and `conn=` contracts.
**X2 total: 10 failures, two production changes** (G4, G6) and three test-side updates
(G3, G5, G7).

**The release signal:** 21 of 23 failures are stale tests chasing a correct and deliberate
tightening of lease authority. **The 2 that are not — G4 and G6 — are both silent safety
regressions introduced by the same migration commit `d486d4a`, and both weaken exactly the
properties the shared baseline forbids weakening** (double-apply protection; honest attempt
accounting). Those two should be treated as release-gating. The other 21 are hygiene.

---

## Residual risk and limits of this triage

- **G6's second assertion is unverified.** `assert events == 0`
  (`tests/test_browser_readiness_preflight.py:134`) may fail once the `attempts` refund is
  restored, because `_terminalize` emits an `apply_result_events` row. Confirming that requires
  applying the fix, which this read-only lane may not do. Called out in G6.
- **G5's dropped jitter is an open question, not a finding.** The `* (0.7 + random()*0.7)`
  multiplier present at `queue.py:260` has no counterpart in `fleet_worker_lease_ats`. I could
  not determine from the code whether that was deliberate. It does not cause any failure in this
  lane. Stated in G5 with what would settle it.
- **Only the seven bracketed commands were run.** The known broad result is
  `3943 passed, 17 skipped, 34 failed` (`-n 4`); this lane accounts for **23** of those 34
  serially. The remaining ~11 live in other lanes' file sets (C1 traced a cluster of them to
  `pgqueue.py:161`). Numbers may not add up exactly, because the 34 was measured under xdist and
  this lane deliberately ran serial.
- **Group verdicts rest on git archaeology of one commit.** G4 and G6 both trace to `d486d4a`
  ("wip: snapshot canonical brain phase one"), a WIP snapshot. If more behavior was dropped in
  that commit outside this lane's ten files, this triage would not have seen it. **A targeted
  review of `d486d4a`'s full diff for other silently-dropped `UPDATE` clauses is recommended and
  is out of scope here.** That is the largest residual risk in this report.
- **No production Postgres was read or written.** Every Postgres assertion above is from the
  disposable per-test fixture instance.
