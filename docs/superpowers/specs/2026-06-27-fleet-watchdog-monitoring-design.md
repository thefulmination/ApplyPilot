# Fleet Watchdog & Monitoring — Design Spec

**Date:** 2026-06-27
**Status:** design, pending review
**Repo:** `New project/ApplyPilot` (Python tool)
**Depends on:** the tested fleet v3 foundation (`src/applypilot/fleet/`, 110 PG tests). See
[`2026-06-26-distributed-residential-fleet-design.md`](2026-06-26-distributed-residential-fleet-design.md).

## 1. Goal & success criteria

Make the fleet safe to leave **unattended**: a process that continuously recovers from the
operational failures the foundation already knows how to handle, plus a periodic judgment layer
that reports health and takes a small set of bounded corrective actions — with everything risky
escalated to the owner.

Two layers, because not every fix should cost tokens:

- **A. Deterministic watchdog** — a no-LLM Python loop that *runs* the recovery primitives the
  foundation built but nothing currently schedules.
- **B. Bounded Claude monitor** — a scheduled agent that reads the telemetry, writes a health
  report, flags anomalies, and may take only **allowlisted** actions.

**Done when:** (A) `watchdog_tick` provably reclaims crashed leases, recovers expired breakers,
trips breakers on rising challenge rates, restarts/quarantines stuck workers, and pauses the fleet
on a cap breach — verified against seeded Postgres state; and (B) the monitor produces a health
report and its action surface is *proven* (by test) to be unable to perform any denied operation.

**Non-goals:** a dashboard UI (telemetry + report are text for now); any apply-decision authority
(that stays with the approval gate + canary).

## 2. Layer A — deterministic watchdog (the workhorse)

A single testable function `watchdog_tick(conn, cfg) -> dict` (returns a summary of what it did),
driven by `run_watchdog(conn_factory, cfg, *, stop=None)` on a cadence (~20–30s) via an
`applypilot-fleet-watchdog` entrypoint, on the broker/home box. Each tick, in order:

1. **Reclaim crashed leases**: `reclaim_compute`, `reclaim_search`, and apply
   `reclaim_stale_leases` — requeue expired leases. (Parked challenges are already frozen out of
   reclaim by `park_challenge`; the watchdog must not touch them.)
2. **Trip breakers proactively**: `governor.evaluate_breakers` — the foundation built this but
   nothing calls it on a clock; the watchdog does, so a rising `challenge_rate` throttles/pauses a
   scope *before* a hard block.
3. **Recover breakers**: `governor.clear_expired_breakers` — restore throttled/paused scopes whose
   cooldown passed (demoted scopes stay sticky).
4. **Roll the window** (nightly only, guarded by a last-roll timestamp): `governor.roll_window`.
5. **Stuck workers**: `heartbeat.detect_stuck` → for each, `issue_command(worker, 'restart')`; if a
   worker's current job has crashed ≥ N times, `quarantine_job` it.
6. **Cap enforcement**: if `cost_cap_total_usd` is breached, set `fleet_config.paused = true`
   (leasing already halts on the cap; this makes the halt explicit + visible).

`cfg` carries the thresholds (heartbeat timeout, job-max, breaker params, restart strike count,
cadence, nightly-roll hour). The watchdog beats its own liveness each tick by reusing
`worker_heartbeat` with a reserved id (`worker_id='watchdog'`, `role='watchdog'`) — no new table —
so a dead watchdog is itself visible via `detect_stuck`/`dashboard_snapshot`.

## 3. Layer B — bounded Claude monitor (the judgment layer)

A scheduled routine (via `/schedule` — a cron cloud agent that fires even when the owner is away;
or `/loop` in-session) that periodically:

1. Reads `heartbeat.dashboard_snapshot(conn)` + the ledgers (`llm_usage`, `rate_governor`,
   queue depths, `auth_challenge` backlog, `poison_jobs`, `applied_set`, recent outcomes).
2. Writes a **health report** (text): healthy/degrading per machine + scope, spend vs cap, queue
   depths, captcha backlog, anomalies (a host with a spiking `challenge_rate`, scoring drift, a
   worker offline, cost nearing the cap).
3. Takes only **allowlisted** actions, and routes everything else to a "needs your decision"
   section of the report.

### 3.1 The guardrail (allow / deny — enforced in code)

| Monitor MAY auto-do | Monitor MUST escalate (never auto-do) |
|---|---|
| pause a host/board scope (`evaluate_breakers`-style pause) | resume the LinkedIn lane / any paused scope tied to it |
| quarantine a poison job | raise or clear a cost cap |
| restart a stuck worker (`issue_command`) | release/resolve a parked challenge (`resolve_challenge`) |
| emit reports / alerts | approve jobs / change approval policy |
| | anything that causes an apply to go out |

The allow-set is exposed through a `MonitorActions` wrapper that **only** binds the safe operations;
the denied operations are not reachable from it. A test asserts the denied ops are absent from the
wrapper's surface (defense in depth beyond prompt instructions).

### 3.2 Honest constraints
The monitor depends on the scheduling infrastructure, and a headless/cron run may lack the owner's
interactive MCP auth — so it is fundamentally **read + report + a few narrow safe actions**, with
the dangerous decisions escalated. Layer A (deterministic) is what keeps the fleet up; Layer B is a
periodic second opinion, not load-bearing.

## 4. Relationship to the compute lane
The watchdog reclaims **compute** leases and enforces the **cost cap**, so it directly backs the
[compute lane](2026-06-27-fleet-compute-lane-design.md). They are complementary and can be built in
parallel; the watchdog should be running the moment the compute fleet runs unattended.

## 5. Testing
- `watchdog_tick` against seeded Postgres: expired compute/search/apply leases → reclaimed; an
  expired breaker → recovered; a scope past the challenge threshold → tripped; a stale-heartbeat
  worker → restart command issued; a job at the crash threshold → quarantined; total-cap breach →
  `paused=true`; a parked challenge → **left untouched**.
- `MonitorActions`: each allow op works; a test proves the deny ops are not on the surface.
- Health-report generator: against a seeded snapshot, asserts the report includes each section and
  correctly flags a seeded anomaly (e.g., a host with `challenge_rate` above threshold).

## 6. Owner-run (not code)
Run the watchdog as a service on the broker/home box; schedule the Claude monitor (via `/schedule`)
once a live fleet exists to watch.

## 7. Decided questions
- Build both layers now, as a dedicated sub-project (separate from compute). **Decided.**
- Monitor autonomy is bounded by the allow/deny table; risky actions escalate. **Decided.**
- No dashboard UI in this build. **Decided.**
