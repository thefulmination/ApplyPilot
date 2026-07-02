# ApplyPilot — Definitive Build Plan: Continuous, Automatic, Self-Correcting Apply Loop

> Produced by a 9-agent end-to-end audit (4 dimension auditors + account-safety + completeness + 2 synthesis + merge), 2026-06-30. Grounded in live code + Postgres/home-brain state.

**Premise (verified live, audit time):** The fleet is dead right now. All 8 apply workers stale 12–13h in `state='applying'`; watchdog idle 12h; 1,540 approved `queued` rows unleased; `canary_remaining=166`; `paused=f`, `ats_paused=f`. Nothing is applying. The system is not under-built — it is **un-wired and un-scheduled**: every control-loop component exists on disk (sensor, classifier, decider, actuator, recovery, learner) but none are connected into a loop, and **zero** Windows scheduled tasks run any of them. The fix is to stand up the loop and connect existing components as stages — not green-field building.

---

## 1. TARGET ARCHITECTURE — the closed-loop controller

A single control loop with eight stages, each filled by a component that **already exists**. The loop has one control channel (`fleet_desired_state`), one actuator (`fleet-agent.ps1`), and runs 24/7 via Windows Task Scheduler.

```
                    CONTROL PLANE (home box, always-on tasks)
   ┌──────────────────────────────────────────────────────────────────┐
   │  SENSORS                CLASSIFY        DECIDE         INTENT       │
   │  heartbeat ─┐                                                       │
   │  llm_usage ─┼──────────▶ Diagnoser ──▶ Doctor ──▶ writes           │
   │  inbox_outcomes ┘        (taxonomy)   (conservative  fleet_desired_ │
   │  apply_queue                           auto-fix)      state         │
   └───────────────────────────────────────────────────────┬───────────┘
                                                             │ (the ONE channel)
                            ┌────────────────────────────────▼───────────┐
                            │  fleet-agent.ps1  (ACTUATOR, per machine)   │
                            │  polls desired_state q20s, starts/stops/    │
                            │  rotates-model local workers to match       │
                            └────────────────────────┬────────────────────┘
   DATA PLANE (m2 / m4 / Railway, scaled by agent)   │
   ┌──────────────────────────────────────────────────▼──────────────────┐
   │  PRIORITIZE (expiry+freshness) ─▶ LIVENESS-GATE ─▶ APPLY (canary+caps)│
   │       offsite/clean-ATS lane, autonomous   │  LinkedIn lane: SEPARATE,│
   │                                            │  human-gated, no actuator│
   └────────────────────────────────────────────┼─────────────────────────┘
                                                 │ outcomes (Gmail)
   LEARN (home box, nightly)                     ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ scan ─▶ email_events ─▶ reconcile ─▶ inbox_outcomes + applied_set      │
   │ research_scores ─▶ [PROMOTION] ─▶ jobs.research_fit_score ─▶ apply order│
   └──────────────────────────────────────────────────────────────────────┘
```

| # | Loop stage | Existing component / entrypoint | Runs it 24/7 (machine) | Current status |
|---|---|---|---|---|
| 1 | **Stay alive** | `fleet-agent.ps1` (actuator) + `applypilot-fleet-watchdog` | scheduled task on home + m2 + m4 (agent); home (watchdog) | actuator unregistered everywhere; watchdog talks to dead channel |
| 2 | **Prioritize (expiry-aware)** | `_LEASE_APPLY` / `acquire_job` ORDER BY | inside workers (data plane) | score-only, no freshness field exists |
| 3 | **Liveness-gate** | `liveness.py` + `APPLYPILOT_PREFLIGHT_LIVENESS` | inside workers, set by canary loaders | built, off by default |
| 4 | **Apply within caps** | apply workers + canary + `rate_governor` | m2/m4/Railway | running when alive; LinkedIn human-gated |
| 5 | **Observe outcomes** | `applypilot outcomes-scan` → `applypilot-fleet-reconcile-email` + `inbox_outcomes` | scheduled task on home (Gmail OAuth lives there) | manual one-shots; PG table missing |
| 6 | **Diagnose** | `diagnoser.py` | invoked by watchdog/Doctor loop | advisory, writes `auto_action=NULL` |
| 7 | **Learn back** | `doctor.py` + `remediator.py` + `research_fit_score` promotion (new) | scheduled task on home | Doctor never ran; promotion wire severed |
| 8 | **Repeat** | Windows Task Scheduler | every machine | **zero fleet tasks registered** |

**The two structural truths that shape everything:**
- **One actuator, one channel.** `fleet-agent.ps1` reads `fleet_desired_state` and is the *only* thing that starts/stops/rotates workers. The watchdog's `remote_commands` channel is vestigial: **1,968 commands issued, 0 acked** because no worker polls it. We **deprecate `remote_commands`** and route all intent (watchdog + Doctor) through `fleet_desired_state`. We do **not** wire `poll_commands` into the worker loop — that would resurrect the dead channel and create a competing second actuator.
- **The decision plane is blind in two senses.** `llm_usage` = 0 rows (cost), `inbox_outcomes` table absent (response-rate). All caps `0.00`, `doctor_last_pass_at` NULL. A decider with null inputs that has never run.

---

## 2. CURRENT vs TARGET — per-dimension gap table

| Dimension | Current (verified) | Target | Binding gap |
|---|---|---|---|
| **Uptime / self-heal** | Fleet dead ~6h overnight; watchdog issued 1,968 restart cmds, **0 acked**; watchdog itself died at 02:00 (no keepalive); usage-wall → worker sleeps 5s and re-hits wall forever; no alerting | Workers self-restart via `fleet-agent`←`fleet_desired_state`; watchdog+Doctor always-on tasks; usage-wall → process exits → relaunched on rotated model; push alert on silence | No actuator scheduled; wrong control channel; no model-rotation; no push alert |
| **Throughput / expiry** | `ORDER BY score DESC` only; **no posted_date/expires_at field in schema at all**; top score-10 tier avgs 32d old, applied before fresh 9.8-tier; discovery never ran (290 tasks `last_run_at=NULL`, 0 discovered); liveness probe off; 113 expired leases | Freshness breaks ties at equal fit; liveness-gate skips dead reqs; discovery feeds fresh supply every 6h | No freshness signal in schema; discovery unscheduled; liveness off |
| **Learning loop** | Diagnoser writes `auto_action=NULL` (advisory only, 13 rows rot); Doctor never ran; Remediator 0 actions; **`research_scores`=0 / `research_fit_score` NULL everywhere — TS tree output never reaches the live engine**; `scoreFeedbackPolicy` never invoked | Diagnoser→Doctor acts; Remediator re-queues safe casualties (with applied_set cleanup); nightly promotion `research_scores`→`research_fit_score`; outcomes→scoreFeedbackPolicy | Severed data-path (scoring leg); no scheduled deciders; Remediator no-op without applied_set delete |
| **Observability** | 39% of attempts `crash_unconfirmed`/`no_result_line`, parked permanently in `applied_set` blocking re-apply; Gmail scan + reconcile manual one-shots; `inbox_outcomes` **absent**; `llm_usage` empty so all cost caps see $0 ($140 actually spent) | Continuous scan+reconcile; `inbox_outcomes` mirrors to PG; `got_response` tracked; `llm_usage` populated → functional cap | Missing table; cost written to ephemeral TEMP SQLite; scan/reconcile unscheduled |

---

## 3. SEQUENCED ROADMAP

Ordered by **jobs-saved-per-unit-effort**, in strict dependency order. Tiers 0–1 are ops/scheduling only (no code) and restore the loop in ~1 session. Tiers 2–4 are code/schema changes that deepen it.

### PHASE 0 — Stop the bleeding (manual, today, no code) — *owner runs*

| Step | Gap closed | Entrypoint / action | Safety guardrail | Leverage | Effort | Done = |
|---|---|---|---|---|---|---|
| 0.1 Restart data plane off Codex | Uptime: 100% throughput | Kill stale `applypilot-fleet-apply` PIDs (home/m2/m4); relaunch via `load-canary-home.ps1`/`load-canary-remote.ps1` on **Claude/Sonnet** (Codex-Spark is walled) | **Do NOT touch LinkedIn lane**; `home-linkedin-0` stays idle, `linkedin_canary_remaining=0` correctly blocks | Critical | minutes | `worker_heartbeat` shows fresh `applying` beats < 2 min old; `applied_at` advancing |
| 0.2 Recover 59 usage-limit casualties | Throughput: parked-safe backlog | `applypilot-fleet-remediator --dsn "$FLEET_PG_DSN" --window-minutes 720` | **Only after 2.4 applied_set-delete fix exists** — else re-queue is immediately re-blocked. If not yet fixed, defer to Phase 2 | High | S | `remediation_actions` > 0; those 59 rows back to `queued` AND absent from `applied_set` |
| 0.3 Flush outcome backlog | Observability: phantom crash rows | `applypilot outcomes-scan` then `applypilot-fleet-reconcile-email --apply` | Scope `apply_queue` only, never `linkedin_queue`; `--dsn` = fleet PG | High | S | `email_reconcile_actions` count rises; crash_unconfirmed count drops |
| 0.4 LinkedIn safety check | Account safety | Confirm the 1 leased `linkedin_queue` row resolved to crash_unconfirmed/closed; verify no LinkedIn worker running | **Never re-arm canary** without human inspection | Critical | S | 0 live LinkedIn leases; canary still 0 |

### PHASE 1 — Restore the loop (scheduling only, no code) ← *highest leverage* — *owner runs the registration*

This tier is the difference between "inert components" and "a running loop." All ops, ~1 hour. Deliver as **one** `register-fleet-tasks.ps1` (the agent can author this script; the owner runs it elevated in his real env).

| Step | Gap closed | Entrypoint / scheduled task | Dependency | Safety guardrail | Leverage | Effort | Done = |
|---|---|---|---|---|---|---|---|
| 1.1 Register `fleet-agent.ps1` everywhere | Uptime: the missing actuator behind the whole 6h outage | Task on **home + m2 + m4**: at-logon, restart-on-failure, no time limit (register cmds in `fleet-agent.ps1:7-11`) | **Verify `fleet_desired_state` real columns first** (`\d fleet_desired_state` — no `machine_id` as audit assumed) | `fleet_desired_state` must carry **no** LinkedIn worker-count field the agent can scale; document in the .ps1 | Critical | S | Set `desired_workers` from home → workers start/stop on m2 within ~30s |
| 1.2 Register watchdog (always-on) | Uptime: watchdog died with no keepalive | Task on home: `applypilot-fleet-watchdog`, at-logon, restart-on-failure (not the closeable `-WithWatchdog` window) | 1.1 | Watchdog `roll_window` keeps `last_window_roll_at` guard (never reset LinkedIn `count_24h` >1/24h); never touch `fleet_config.paused` | Critical | S | Kill the watchdog process → it auto-relaunches; `last_beat` stays fresh |
| 1.3 Register Doctor + Remediator loop | Learning: deciders never ran | Task on home: `run-fleet-doctor.ps1` every 5 min; fold Remediator in (or own 30-min task) | 1.1; **2.1 llm_usage** (else cost invariant blind) | Extend Doctor `_FORBIDDEN_ACTUATORS` to reject any `scope_key` containing `linkedin`; pause → `ats_paused` only | Medium | S | `doctor_last_pass_at` advances; conservative knobs appear in `fleet_knobs` |
| 1.4 Register scan+reconcile chain | Observability: outcome truth is manual | Task on home, every 6h: `applypilot outcomes-scan` → chained `applypilot-fleet-reconcile-email --apply` | Gmail OAuth on home; `APPLYPILOT_ENABLE_GMAIL_MCP=1`, `.conda-env` python, `APPLYPILOT_DB_PATH`→LOCALAPPDATA brain | `apply_queue` scope only; `--dsn` = fleet PG | High | S | `email_events.scanned_at` advances every 6h with no human |
| 1.5 Register discovery loop | Throughput: queue 84% stale, 0 new postings | Task on home or m2 (3× RAM): `run-discovery-home-loop.ps1` every 6h | 1.1 | **REQUIRED `--proxy`** if the machine also runs apply/LinkedIn workers; never use apply Chrome profile to scrape | Critical (supply) | S | `discovered_postings` count > 0; `search_tasks.last_run_at` populated |

### PHASE 2 — Give the decision plane its senses (small additive code) — *agent builds*

| Step | Gap closed | Components touched | Dependency | Safety guardrail | Leverage | Effort | Done = |
|---|---|---|---|---|---|---|---|
| 2.1 Write cost to PG `llm_usage` | Observability/uptime: caps see $0 while $140 spent | `queue.py:103-148` `write_apply_result()` — also INSERT `est_cost_usd` into `llm_usage` (mirror `write_compute_result`) | — | — | High | S | After one apply, `llm_usage` has a row; `_total_cap_breached` reads non-zero |
| 2.2 Set real cost cap | Uptime: no circuit breaker | `fleet_config.cost_cap_daily_usd` = $50–75 (~140–200 applies at $0.36) | 2.1 | — | Medium | S | Simulated overspend trips the cap; workers pause on `ats_paused` |
| 2.3 Create `inbox_outcomes` + wire push | Observability/learning: Doctor response-rate blind | Apply DDL `schema_v3.sql:269-284`; wire `push_inbox_outcomes()` into home sync; set `applied_set.got_response=true` on reconcile | — | — | High | S | `inbox_outcomes` exists; rows mirror after a scan; `push_inbox_outcomes()` no longer throws |
| 2.4 Remediator deletes dedup_key from `applied_set` | Throughput: re-queued rows unleasable | `remediator.py` re-queue SQL — DELETE matching `applied_set` key in same transaction | — | LinkedIn crash rows out of scope | High | S | A re-queued usage_limit row leases successfully (not blocked by dedup) |

### PHASE 3 — Expiry-aware throughput (schema migration + ordering) — *agent builds*

| Step | Gap closed | Components touched | Dependency | Safety guardrail | Leverage | Effort | Done = |
|---|---|---|---|---|---|---|---|
| 3.1 Add `posted_date` field | Throughput: no freshness signal exists | `jobs` + `apply_queue` schema; populate in `store_jobspy_results` from JobSpy `date_posted` + ATS API | — | — | High | M | `posted_date` non-null on newly-discovered jobs |
| 3.2 Freshness-aware ORDER BY | Throughput: 32d reqs applied before fresh | `_LEASE_APPLY` (`queue.py:72`), `acquire_job` (`launcher.py:693`), `_PUSH_APPLY_SELECT` (`sync.py:44-55`): `score DESC, COALESCE(posted_date, discovered_at) DESC, url` | 3.1 | — | High | M | At equal score, freshest job leases first |
| 3.3 Turn on liveness pre-probe | Throughput: 15% dead-req launches | `APPLYPILOT_PREFLIGHT_LIVENESS=1` in `load-canary-home.ps1`/`load-canary-remote.ps1` | — | **Add `linkedin.com` early-return guard in `liveness.py`** + 1–3s probe jitter (no probe+apply burst on same IP) | Medium | S | Dead reqs skipped pre-Chrome; closed-req launches drop |
| 3.4 Backfill 408 June-29 no_result_line | Observability: 408 jobs locked out | Verify `tool_calls==0` from per-job log; reset to `queued`, remove from `applied_set` | 2.4 (applied_set delete) | — | Medium | M | ~408 rows re-queued and leasable; not double-applied |

### PHASE 4 — Close the learning wire (strategic payoff) — *agent builds, owner schedules*

| Step | Gap closed | Components touched | Dependency | Safety guardrail | Leverage | Effort | Done = |
|---|---|---|---|---|---|---|---|
| 4.1 `research_scores → research_fit_score` promotion | Learning: **entire TS tree is a dead investment** | New scheduled CLI: read newest-per-url `research_scores`, UPDATE `jobs.research_fit_score`+`research_decision` | — | Research stays **advisory** (tie-break/re-rank, never primary gate) per unified-brain spec | High | M | `research_fit_score` non-null; apply order shifts measurably |
| 4.2 Wire `email_events → scoreFeedbackPolicy` | Learning: outcomes don't tune scoring | Nightly CLI: `brainDb.readEmailEvents()` → `buildScoreFeedbackPolicyFromOutcomes()` → write `outcomePolicyPath` → load in scoring | 4.1, 2.3 | — | High | M | Silent-reject companies get score-capped on next scoring pass |
| 4.3 Diagnoser → Doctor auto-act on usage-wall | Uptime: the exact 6h outage | On fleet-wide usage-limit, Doctor writes `fleet_desired_state` to rotate model (NOT `remote_commands`); worker exits after N consecutive `USAGE_LIMIT_STATUS` | 1.1, 1.3 | Gate to **ATS role only**: assert `role != 'linkedin'` in any auto-restart path | High | M | Inject usage-wall → fleet rotates to Claude and resumes with no human |
| 4.4 Push alerting | Uptime: 6h silent outage | New 5-min health-check task: all heartbeats stale / watchdog silent / throughput zero → Pushover/Twilio/webhook | 1.2 | — | High | M | Stop all workers → owner's phone pings within ~10 min |

### PHASE 5 — Cost/quality optimization (deferred) — *agent builds*

Route ~60% of clean-ATS (Greenhouse/Ashby/Lever) applies to a cheaper tier (Haiku) via the `apply_domain` column; reserve Sonnet for complex multi-page forms. Target ~$0.15–0.20/apply. **Only after 2.1/2.2 give real cost visibility** — never optimize a metric you can't measure.

---

## 4. DO-THIS-FIRST — top 5 concrete next actions

| # | Action | Exact starting point | Who runs it |
|---|---|---|---|
| 1 | **Restart the dead fleet off Codex** | Kill stale PIDs; `load-canary-home.ps1` / `load-canary-remote.ps1` with agent=Claude/Sonnet. **LinkedIn untouched.** | **OWNER** — real env, his machines |
| 2 | **Author `register-fleet-tasks.ps1`** registering all Phase-1 tasks (fleet-agent on home/m2/m4; watchdog; Doctor 5-min; scan+reconcile 6h; discovery 6h) | New file; register cmds from `fleet-agent.ps1:7-11`; pattern from `register-keepalive.ps1` | **AGENT builds the script** → **OWNER runs it elevated** per machine |
| 3 | **Verify `fleet_desired_state` real schema** before any write path targets it | `psql … -c "\d fleet_desired_state"` (audit assumed a `machine_id` column that does not exist) | **AGENT** (read-only psql) |
| 4 | **Build the 4 small Phase-2 code fixes** (`llm_usage` write in `queue.py`; `inbox_outcomes` DDL + `push_inbox_outcomes` wiring; Remediator `applied_set` delete) | `queue.py:103-148`, `schema_v3.sql:269-284`, `sync.py:569-596`, `remediator.py` | **AGENT builds** → owner deploys (editable install, `.conda-env`) |
| 5 | **Manual scan + reconcile once** to flush the crash backlog now | `applypilot outcomes-scan` → `applypilot-fleet-reconcile-email --apply` (`APPLYPILOT_ENABLE_GMAIL_MCP=1`, home Gmail token) | **OWNER** — Gmail OAuth + real `%LOCALAPPDATA%` brain |

**Agent can build now (no real-env access needed):** `register-fleet-tasks.ps1`, all Phase 2 code, the read-only schema verification, Phase 3 schema+ordering, the Phase 4.1 promotion CLI, and the 4.4 health-check script. **Owner must run in his real env:** killing/relaunching workers, registering scheduled tasks (elevated), the Gmail scan (OAuth + AppData overlay caveat — Bash writes to AppData hit the app's private overlay, not the real folder), and any LinkedIn canary go-live.

---

## 5. SAFETY & HUMAN-IN-LOOP — non-negotiable guardrails

These hold no matter how autonomous the loop becomes. They are **structural boundaries, not runtime conventions.**

1. **LinkedIn lane is human-gated end-to-end.** Canary arm, approval, and worker process start each require explicit human action. No automated path (watchdog, Doctor, Remediator, fleet-agent, model-rotation) may initiate or restart a LinkedIn apply. `fleet_desired_state` must carry **no** LinkedIn worker-count field the agent can scale.
2. **`linkedin_canary_remaining=0` is the correct automatic block** — never auto-reset to a positive value by any automation. Re-arming is human-only.
3. **`fleet_config.paused` is off-limits to all automation** — it halts both lanes. Doctor/watchdog ATS-only pauses target `ats_paused`, which the LinkedIn worker never reads.
4. **One-IP LinkedIn rule preserved** — `lease_linkedin()` rejects any worker whose `public_ip ≠ owner_ip` (`queue.py:437`); never weakened by any restart/rotation path. Add a `role != 'apply'` assertion if auto-restart lands in a shared `WorkerLoop`.
5. **Rolling 24h LinkedIn cap** (`daily_cap=20`, `min_gap_seconds=1200`) preserved; watchdog `roll_window` keeps `last_window_roll_at` guard (no double-reset).
6. **Discovery never scrapes from the apply/LinkedIn IP** — `--proxy` REQUIRED on any machine that also runs apply/LinkedIn workers; never reuse the apply Chrome profile.
7. **Liveness probes never hit LinkedIn** — `liveness.py` early-returns on `linkedin.com`; jitter probes to avoid probe+apply bursts on one ATS IP.
8. **Doctor `host_skip` guard extended** — reject any `scope_key` containing `linkedin` (closes the theoretical `host:linkedin.com` gap).
9. **Canary auto-pause + spend cap remain the ATS blast-radius bound** — keep them functional (Phase 2.2 makes the cost cap real); offsite/clean-ATS may run aggressively within them.
10. **Self-hosted runtime only** — an AI session is never the always-on runtime; uptime depends on Windows scheduled tasks + fleet-agent + watchdog on the owner's machines.

---

**One-line summary:** ApplyPilot has every control-loop component and connects none of them, and schedules none of them. Phase 1 (register the tasks, route everything through the one working actuator `fleet-agent`←`fleet_desired_state`, deprecate the dead `remote_commands` channel) makes the loop *run at all* with no code; Phases 2–4 give the decider its cost/outcome senses, add the missing `posted_date` freshness field, and build the single `research_scores → research_fit_score` promotion wire that makes the entire downstream TS investment non-vestigial — all while LinkedIn stays a physically separate, human-gated lane with no automated actuator.


---

# AMENDMENT SET v2 (2026-07-01) — VERIFIED CORRECTIONS; SUPERSEDES CONFLICTING TEXT ABOVE

> Produced by an 8-agent verification pass (5 claim-cluster verifiers against real code/live DBs + 2 red-teamers + synthesis).
> 43 claims checked; 19 corrected. Where this section conflicts with the plan above, THIS SECTION GOVERNS.



Target: `C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot\docs\superpowers\specs\2026-06-30-autonomous-apply-loop-roadmap.md`

This is an amendment contract, not a rewrite. Apply each item verbatim against the named phase.

---

## 1. VERDICT

**YES — sound to implement after amendments.** The plan's diagnosis (fleet dies overnight → 6h outage; observability rot; empty learning loop) is correct and the pre-built infrastructure exists; but four steps as written are self-defeating (Phase 1.1 kills Phase 0.1's workers; Phase 0.2's window selects zero rows; Phase 4.3 rotation force-kills mid-apply; three "build" items are already shipped) and must be resequenced/corrected before execution.

---

## 2. FACTUAL CORRECTIONS

Each row: **plan claim → corrected text** (evidence).

**C1 — Gmail env var (Phase 1.4).**
Plan lists `APPLYPILOT_ENABLE_GMAIL_MCP=1` as a scan-task dependency → **DROP IT.** That var only gates the apply-agent's MCP tools (`launcher.py:175,221`, `prompt.py:582`); `outcomes-scan` never reads it. Real deps: pre-authorized `gmail_credentials.json`+`gmail_token.json` in APP_DIR (`gmail_outcomes.py:709-712`), `APPLYPILOT_DB_PATH`→`%LOCALAPPDATA%\ApplyPilot\applypilot.db`, `.conda-env` python, and — for LLM-quality (not heuristic) extraction — any one of `GEMINI_API_KEY`/`OPENAI_API_KEY`/`DEEPSEEK_API_KEY`/`LLM_URL`. Extraction silently degrades to a deterministic heuristic on any missing-key/error (`outcome_extract.py:88-93,104-109`) — so "needs DeepSeek" is an overstatement, but heuristic-mode is a phantom-green quality drop.

**C2 — Watchdog channel reality (Phase 1.2, §1 table).**
Plan says only Doctor talks to the dead channel; "register watchdog, no code." → **Watchdog's ONLY actuator (`_handle_stuck`→`heartbeat.issue_command(...,"restart")`, `watchdog.py:192`→`INSERT INTO remote_commands`, `heartbeat.py:212-224`) is ALSO on the dead channel** (1,968 issued / 0 acked; nothing consumes it). After Phase 1.2 the watchdog detects stuck workers and issues restarts into the void. Its roll_window/cap duties DO work; its restart leg does not. Correct the table row and pick a disposition (see §3, S6).

**C3 — fleet_desired_state key + model rotation (Phase 1.1, 4.3).**
`fleet_desired_state` key = `machine_owner` (PK); columns `(desired_workers, agent, model, generation, updated_by, updated_at)`. There is **NO linkedin/role field** — no actuator path can start a LinkedIn worker (`run-fleet-worker.ps1` hardcodes `applypilot-fleet-apply.exe`, "LinkedIn never runs here"; LinkedIn is a structurally separate binary `applypilot-fleet-linkedin`, `pyproject.toml:60-61`). Model rotation IS supported — via `generation` bump (`fleet-agent.ps1:96` guard `$gen -ne $lastGen` → kill+respawn on new model). `run-fleet-worker.ps1` is a dumb launcher taking `-Agent/-Model/-Label/-Slot` CLI params; the agent supplies them from the polled row.

**C4 — usage-limit behavior (Phase 4.3 premise).**
Plan: "worker sleeps 5s and re-hits the wall forever." → **WRONG.** Worker already exits after `USAGE_LIMIT_MAX_STREAK=3` consecutive walls with `USAGE_LIMIT_COOLDOWN=30s` between (both env-tunable) and re-queues (not parks) each hit (`apply_worker_main.py:105-108,351-363`). The gap Phase 4.3 must close is **model ROTATION on repeated walls** (generation bump), not an infinite-sleep bug — the process-exit logic already exists.

**C5 — freshness proxies (Phase 3.1/3.2).**
Plan mandates a new `posted_date` schema migration on two DBs. → **No migration needed for the tie-break.** `jobs.discovered_at` already exists and is already read in an exclusion filter (`launcher.py:606-616`, `APPLYPILOT_MAX_JOB_AGE_DAYS`); `apply_queue.pushed_at` (PG, `NOT NULL DEFAULT now()`) is set at push-time. Phase 3.2's own target ordering already names `discovered_at` as the fallback. Ship 3.2 against `COALESCE(pushed_at/discovered_at)`; treat true `posted_date` (JobSpy drops `date_posted` today — `jobspy.py:153-220`) as a later optional refinement, not a gate.

**C6 — research_fit_score consumer reality (Phase 4.1).**
Plan: "populate `research_fit_score` → apply order shifts measurably." → **WRONG as stated.** `research_fit_score` is read in exactly ONE dead-end lane (`frontier_select.py`, writes to its own `frontier_scores` table, never back to jobs). The three real apply-order queries — `_LEASE_APPLY` (`queue.py:30`), `_PUSH_APPLY_SELECT` (`sync.py:44-56`, which sets `apply_queue.score`), `acquire_job` (`launcher.py`) — **never reference `research_fit_score`.** Promotion must write into `fit_score`/`audit_score` directly (or extend those three ORDER BYs); populating `research_fit_score` alone changes nothing downstream. Note: `acquire_job`'s ORDER BY is already a 7-key chain, not score-only — the freshness key must splice into it.

**C7 — Doctor is cost-blind, not inbox-dependent (Phase 1.3, 2.x).**
Plan implies Doctor reads `inbox_outcomes`/response-rate and is "blind" without `llm_usage`. → **WRONG.** `doctor.py` has ZERO cost/`llm_usage`/`inbox_outcomes`/response-rate references (grep empty); it reads only `apply_queue` failure rows in a 60-min window (`doctor.py:342,373-376`). `_total_cap_breached` lives in `watchdog.py:143`, not Doctor, and is inert because the cap itself is `0.00` (treats 0 as "no cap"), not because `llm_usage` is empty. Doctor needs no cost data to run.

**C8 — liveness linkedin guard already exists (Phase 3.3, §5.7).**
Plan frames "add a linkedin.com early-return guard" as new work. → **Already shipped.** `liveness.py:60` `BLOCKED_HOSTS` includes `linkedin.com`; `_dispatch` (`:280-283`) early-returns UNCERTAIN before any HTTP probe. Reframe as "verify and preserve; add a regression test asserting no HTTP call for linkedin.com."

**C9 — Doctor LinkedIn actuator guard already exists (Phase 1.3, §5.8).**
Plan lists "extend `_FORBIDDEN_ACTUATORS` to reject any scope_key containing linkedin" as a build step. → **Already shipped** (commit `79883a0`). `_mentions_linkedin()` gates `lane/scope_key/op/host/url` on every action; `_FORBIDDEN_ACTUATORS` + `ats_paused`-only pause routing are live. Delete this line item; reframe as "verify."

**C10 — cost DB path attribution (Phase 2.1 context).**
Plan attributes ephemeral cost logging to `config/database.py` design default. → **PARTLY wrong.** `config.py:19` defaults to a *persistent* `APP_DIR/applypilot.db`; ephemerality comes from `run-fleet-worker.ps1:50` overriding `APPLYPILOT_DB_PATH` to `$env:TEMP\fleet_apply_throwaway_$Slot.db`. There is no literal `config/database.py`; `config.py` and `database.py` are separate modules.

**C11 — inbox_outcomes is dead code today (Phase 2.3).**
Plan implies something breaks on the missing table. → **`push_inbox_outcomes` (`sync.py:569`) has zero production callers** (only a test). Nothing breaks today; it will raise `UndefinedTable` only once Phase 1.4's chain (or a new caller) invokes it. DDL is at `schema_v3.sql:269-284`. Live PG has a *different* table `inbox_events` — do not conflate.

**C12 — Phase 0.2 window (Phase 0.2).**
Plan: `--window-minutes 720`, "crashes now >12h old." → **The 59 casualties are ~61.6h old** (clustered 2026-06-30 01:37–01:47). `720` selects ZERO. Needs ≥~3,700 min or a status-keyed selection. See §3 S3.

**C13 — discovery script wiring (Phase 1.5).**
Plan cites `run-discovery-home-loop.ps1` and assigns it a `--proxy` requirement. → **WRONG.** That script is ingest-only (`expand`/`pull`, no egress, no `-Proxy` param — passing `-Proxy` errors the task). The actual scraper `run-fleet-discovery.ps1` also has no `-Proxy` param; proxy support lives one layer down (`discovery_main.py:84`, `FLEET_PROXY`). Split into two tasks (see §3 S7).

---

## 3. SEQUENCING AMENDMENTS

**S1 — NEW Phase 0.0 (before everything): set desired_state as source of truth.**
Root cause of the FM-1/FM-2 traps: canary loaders and `fleet-agent` are two competing actuators on the same `<Label>-<Slot>` worker-id namespace, and m2's live row is `desired_workers=0`. Before any worker launch or agent registration, run:
```
UPDATE fleet_desired_state SET desired_workers=1, generation=generation+1, updated_by='roadmap-bringup' WHERE machine_owner='home';
UPDATE fleet_desired_state SET desired_workers=8, generation=generation+1, updated_by='roadmap-bringup' WHERE machine_owner='m2';
UPDATE fleet_desired_state SET desired_workers=2, generation=generation+1, updated_by='roadmap-bringup' WHERE machine_owner='m4';
```
(m2 count = owner's chosen live count; 8 is illustrative.) **Hard rule, documented in `register-fleet-tasks.ps1` and Phase 0.1:** never co-locate a canary loader and `fleet-agent` on one machine — they will kill-fight. Prefer letting the agent spawn from desired_state.

**S2 — Phase 1.1 subsumes Phase 0.1.** Do not hand-kill PIDs then register the agent (the agent's first poll force-kills mismatched workers, `fleet-agent.ps1:108-113`). Instead: register `fleet-agent` (1.1), then bump generation once — the agent's existing kill+respawn cleans stale PIDs and spawns to the S1 counts using the already-correct agent/model. Keep a standalone "resume in 5 min without waiting for the script" note only if the owner wants immediate bring-up; state that 1.1 subsumes 0.1's outcome. **1.1 "Done =" must read the row back and assert `desired_workers>0` per enrolled machine** (else registration = "kill everything on m2").

**S3 — Move Phase 0.2 into Phase 2, after 2.4.** Its own guardrail already defers it (re-queue is re-blocked until the applied_set-delete of 2.4 exists). Replace the hardcoded window with a **status-keyed backfill** (`apply_error ILIKE '%usage_limit%' AND status='failed'`) or a dynamically-computed window (`ceil(EXTRACT(EPOCH FROM (now()-min(updated_at)))/60)+buffer`). "Done =" must verify the specific 59 URLs flipped to `queued` AND are absent from `applied_set`, not merely `remediation_actions>0`.

**S4 — Phase 1.3 (Doctor) has NO dependency on 2.1.** Remove the spurious "2.1 llm_usage else cost-blind" gate (C7). Doctor is safe to schedule immediately; it is cost-agnostic. Guardrails: (a) observe one recommend-only pass on the restarted fleet before trusting auto-fixes; (b) **pin the invariant** `no_result_line`/`stuck`/`suspicious_page` ∈ `_AGENT` (recommend-only, `doctor.py:93`) with a test — this is what prevents an FM-1/FM-6 kill-storm's `crash_unconfirmed` burst from auto-throttling the fleet. Note `--once` returns exit 3 (not 0) on lock contention — health-checks must not treat that as failure.

**S5 — Phase 4.3 rotation must DRAIN, not force-kill; gate it on 2.4 + last-tool-name capture.** `fleet-agent.ps1`'s generation-bump path is `Stop-Process -Force` mid-apply → orphaned leases (`applying` rows unleasable until lease-expiry), fresh `crash_unconfirmed`, and double-apply risk on later re-lease. Two fixes, pick one: (a) rotation signals workers "finish current job then exit" (reuse the existing bounded usage-limit exit discipline) and the agent respawns on `have<want` after clean exit; or (b) if hard-kill is unavoidable, add a **post-kill lease-reclaim sweep scoped to `apply_queue` ONLY** (never `linkedin_queue`) that requeues leases owned by killed worker-ids **only when provably pre-submit** (using the tool_calls/last-tool-name signal — do NOT blanket-requeue `no_result_line`, which may have submitted). **Explicit dependency: land 2.4 + the last-tool-name capture before enabling 4.3 auto-rotation.**

**S6 — Phase 1.2 watchdog: decide the restart leg (C2).** Either (a) drop watchdog's restart intent and rely on `fleet-agent`'s count-reconcile for respawn — reframe 1.2 as "register watchdog for governor/roll_window/cap only, NOT restart"; or (b) small code change (Phase 2) repointing `_handle_stuck` to bump `fleet_desired_state.generation`. Pick one explicitly; correct the "no code" claim to be honest about what watchdog can/cannot do post-registration.

**S7 — Concurrent-session coordination for fleet_desired_state (FM-3).** The table is being written out-of-band today (`updated_by='seed'`/`'switch-m2-to-claude'`, both TODAY). Mandate: **all writers use `generation=generation+1` (never a literal) + a distinct `updated_by`.** Before 4.3 ships, Doctor's rotation UPDATE must be a compare-and-set (`WHERE machine_owner=%s AND generation=%s`, no-op-with-log on loss); add a CHECK/trigger rejecting non-increasing generation. During roadmap execution, the human stops hand-editing desired_state except through S1's documented step.

**S8 — Split Phase 1.5 into scrape + ingest (C13).** (a) Scrape task = `run-fleet-discovery.ps1` on a residential/non-apply-IP machine (m2); owns "Done = `discovered_postings>0`" and `search_tasks.last_run_at` populated. (b) Ingest task = `run-discovery-home-loop.ps1` on home. Do NOT pass `-Proxy` to the ingest script. To make §5.6's proxy guardrail real, add `[string]$Proxy` to `run-fleet-discovery.ps1` threaded to `--proxy`/`FLEET_PROXY` + a hard refusal to scrape if the machine has a live apply/linkedin heartbeat and no proxy set (elevate from "no code" to Phase 2/3 scope).

**S9 — Phase 0.4 Done rewrite (parked-challenge row, FM-11).** The 1 `leased` `linkedin_queue` row (`.../4400830361`) is a deliberate `park_linkedin_challenge` freeze (`apply_status='challenge_pending'`, `lease_expires_at≈2036`) — it NEVER resolves to `crash_unconfirmed`/closed. Rewrite "Done =": *"confirm the leased row is the expected park-challenge freeze — do NOT clear it; confirm `linkedin_canary_remaining=0` and no `home-linkedin-*` heartbeat within 5 min."* Any lease-reclaim sweep must be `apply_queue`-only.

---

## 4. SIMPLIFICATIONS ADOPTED

**ADOPTED:**

- **RT2-1 (reuse `discovered_at`/`pushed_at`, skip the posted_date migration):** ADOPT. Ship Phase 3.2 against existing freshness proxies; make `posted_date` an optional later refinement, not a gate. (C5.)
- **RT2-4 / RT2-Phase-1.1-subsumes-0.1:** ADOPT as S2.
- **RT2-5 (delete the already-shipped Doctor guard line item):** ADOPT. (C9.)
- **RT1-FM-5 (remove Doctor→2.1 dependency):** ADOPT as S4.
- **RT2-2 (defer Phase 4.2 — no interview signal exists yet):** ADOPT. Do not build the brainDb→scoreFeedbackPolicy feed now; `research_scores`=0, `inbox_outcomes` absent, and a *working* JSONL pipeline already exists (`export-outcomes`→`applypilotOutcomeCalibration.ts`→`--outcome-policy=`). Defer 4.2 until 1.4+2.3 have run ≥2–4 weeks and accumulated real outcome labels.
- **RT2-3 (cut Phase 5's dollar target to a backlog note):** ADOPT. No cost baseline exists (`llm_usage`=0, caps=0.00); the `$0.15–0.20/apply` target is a guess dressed as a spec. Keep one backlog line, gated on 2.1/2.2 producing real data.
- **RT-C8 (liveness guard = verify-not-build):** ADOPT as C8/S8-adjacent.

**REJECTED:**

- **RT2-6 (use :8787 console instead of push alerting for Phase 4.4):** REJECT as a full replacement, ADOPT partially. A LAN-only :8787 banner does not ping the owner's phone while away — which is the actual requirement. Keep Phase 4.4's push channel, but take the cheap first cut: reuse the already-authorized Gmail token (present for 1.4) for an email-to-phone alert before provisioning a new Pushover/Twilio credential. Escalate to a dedicated push service only if email proves too slow.
- **Do NOT drop Phase 1.5 scraping** despite its mis-wiring — it is supply-critical (290/290 search_tasks never ran, `discovered_postings`=0). Fix the wiring (S8), don't cut it.

---

## 5. REVISED DO-THIS-FIRST (top 5)

Ranked by value-per-risk. Split **[OWNER]** (manual, needs real env / credentials / can't be automated) vs **[AGENT]** (buildable now).

**1. [OWNER] Set desired_state, then register `fleet-agent` — self-healing fleet (Phase 0.0 + 1.1).**
This alone converts "dies overnight, stays dead" into "self-heals within ~30s." Run S1's three `UPDATE fleet_desired_state` statements (bump generation, `updated_by='roadmap-bringup'`), confirm `desired_workers>0` per machine, then register `fleet-agent.ps1` as an at-logon scheduled task on home/m2/m4. Do NOT also run canary loaders on those machines. Zero new code — the reconciliation is pre-built and verified.

**2. [AGENT] Phase 2.1 — write `est_cost_usd` into `llm_usage` on apply.** Near-zero-risk additive INSERT in `write_apply_result` (`queue.py:103-148`), mirroring `write_compute_result` (`queue.py:281-299,293-297`). Cost already available at the call site (`worker.py:470`). Single unblock for all cost visibility (gates 2.2, Phase 5).

**3. [OWNER] Phase 1.4 — register the scan+reconcile chain (6h).** Fixes the "39% phantom `crash_unconfirmed`" observability rot (those rows can't re-lease). **Deps corrected (C1):** pre-authorized `gmail_credentials.json`+`gmail_token.json` in APP_DIR, `APPLYPILOT_DB_PATH`→real LOCALAPPDATA brain, `.conda-env` python, one LLM key (optional; heuristic fallback otherwise). Run in the owner's real env (not the AppData overlay). Make it fail-loud: assert `email_events.scanned_at` advanced past the task's start; alert on OAuth-token-near-expiry; log LLM-vs-heuristic mode.

**4. [AGENT] Phase 1.3 — register Doctor loop (5 min).** No 2.1 dependency (C7). Ship with the `_AGENT`-recommend-only regression test (S4) and a one-pass recommend-only observation before trusting auto-fixes. `run-fleet-doctor.ps1 -Once` health-check must accept exit code 3 (lock contention) as non-failure.

**5. [OWNER] Phase 1.5 (split) — discovery scrape on m2 + ingest on home (S8).** Supply lane: 290/290 search_tasks never ran, `discovered_postings`=0 — without fresh supply, all downstream ordering optimizes a shrinking pile. Register `run-fleet-discovery.ps1` on m2 (residential IP) and `run-discovery-home-loop.ps1` on home. Do NOT pass `-Proxy` to the ingest script (it errors). Proxy wiring on the scraper is a follow-on [AGENT] code change.

**Explicitly NOT in the first cut:** Phase 3 schema migration (use `discovered_at`/`pushed_at`), Phase 4.2 (no signal yet), Phase 5 (no cost baseline), Phase 4.3 auto-rotation (gate on 2.4 + drain-before-rotate first). Phase 0.2's 59-row recovery moves to Phase 2 after 2.4 with a corrected window.

---

Plan file: `C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot\docs\superpowers\specs\2026-06-30-autonomous-apply-loop-roadmap.md`
Key evidence anchors: `fleet-agent.ps1:71-113,96` · `run-fleet-worker.ps1:9,50` · `doctor.py:62-65,93,342,373-376` · `watchdog.py:143,192` · `queue.py:30,103-148,281-299,437,511` · `sync.py:44-56,569` · `launcher.py:606-616,1141-1151,1284-1339` · `liveness.py:60,280-283` · `apply_worker_main.py:105-108,351-363` · `frontier_select.py` · `outcome_extract.py:88-109` · `gmail_outcomes.py:709-712` · `schema_v3.sql:269-284`.