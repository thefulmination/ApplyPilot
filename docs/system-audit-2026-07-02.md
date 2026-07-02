# ApplyPilot system audit — 2026-07-02

Multi-agent audit (6 dimension auditors + adversarial verification of every critical/high
finding + completeness critic; 29 agents). **All 22 verified findings CONFIRMED, none refuted.**
Scope: the Python apply tool, the res_build TS tree, the ops/control plane, the live SQLite
brain (read-only), and the fleet Postgres (SELECT-only).

## Headline

The machine is fundamentally sound — but it has been **silently OFF since 2026-06-30 ~02:00**
with every gate armed, and **the books don't balance**: 75 real submitted applications exist
only in Postgres while the brain (source of truth) still shows those employers as never-applied.
The funnel's worst leak is **execution, not selection**: 1,540 ready-to-apply offsite jobs
(scores 7.0–10.0) sat idle for 2.5 days because the whole fleet runs on hand-fired bursts and
nothing survives a reboot or alerts on its own death.

## Current state (verified 2026-07-02)

| Fact | Value |
|---|---|
| Brain | 82,096 jobs; 299 applied (latest 6/29); 5,586 gate-passers un-applied (3,416 offsite / 2,170 LinkedIn) |
| Fleet PG | 1,540 queued ats; 1,255 queued LinkedIn (canary=0 → frozen); last apply 6/30 12:20 |
| Sync-back | **813 terminal rows unsynced**, incl. **75 applied** the brain doesn't know about |
| Workers | 10 heartbeats, ALL stale since 6/30 ~02:00; watchdog dead too; desired = home 1 / m2 0 / m4 2 |
| Money | spend caps ALL 0 (= uncapped); est. $140.62 spent ($82.86 on applied); paused=false, canary_remaining=166 |
| Upstream | discovery stopped 6/28; scorer stalled 6/26 — **10,629 enriched never scored** (~880 est. gate-passers stranded) |
| Outcomes | 230 email events: 199 acknowledged / 31 rejected / **0 interviews**; scan last ran 6/30 |
| Bridge | decision_source NULL for all 82,096 — the res_build bridge (built 6/30) has not been run yet |

## 🔴 Critical / do first (all confirmed)

1. **Sync the books before anything applies again** — `python -m applypilot.apply.fleet_sync pull`.
   813 rows behind incl. 75 confirmed applies absent from the brain: any brain-driven apply path
   sees those employers as un-applied (double-apply exposure outside PG dedup), and email
   reconciliation can't match their responses. (S)
2. **Fleet armed + uncapped + unattended.** paused=false, canary 166 remaining, spend caps 0.
   Decide deliberately: set a real spend cap + daily cap, then restart on m2/m4 (home box has the
   Kernel-Power-41 hardware fault) — or pause. Don't leave it armed with nobody watching. (S)
3. **Mystery supervisor ran against the live brain TODAY** — `%LOCALAPPDATA%\ApplyPilot\keepalive.done`
   written 2026-07-02 15:13 ET ("budget reached") by a supervise-apply from a git worktree
   (likely the other Claude session's keepalive). Identify it, and note the stale done-marker
   silently blocks home-lane restarts. (S)
4. **Password rotation STILL pending** and the exposure regenerated: cleartext password in
   OneDrive-synced `.applypilot\profile.json`, and `C:\Users\JStal\m2bundle.zip` (267MB: .env keys +
   password + full brain) still on disk 2 days after deploy despite the delete-after runbook. Also
   stale cloned Chrome worker profiles (Cookies/Login Data) in OneDrive. Rotate → delete → move
   secrets out of OneDrive sync. (S)
5. **profile.json may be answering screening questions wrong on every apply**: it says
   `years_of_experience_total = 5` and `city = San Francisco` — user profile says 9.5 yrs and NJ.
   ~372 applications answered from this file. OWNER must confirm (could be deliberate) — do not
   silently edit. (S)

## 🟠 High — turn it back on properly (this week)

- **Schedule the whole chain** (the single highest-leverage change): daily
  discover → score → verify-live → push → apply (m2/m4) → pull → scan-gmail. Freshness has
  collapsed: zero gate-passers under 8 days old; 348 queued jobs discovered >30 days ago.
- **fleet.ps1 is dead on arrival**: `param([int]$Home...)` collides with PowerShell's read-only
  `$Home` automatic variable — cannot execute in PS 5.1 or pwsh 7. Rename (e.g. `-HomeWorkers`).
  Also: generation bumps only on agent change, so `-Model` changes never restart workers.
- **Nothing survives a reboot**: zero Task Scheduler entries for fleet-agent/selfheal/console.
  Register at-logon tasks per box.
- **Who watches the watcher**: selfheal's durable-safe stop only fires on graceful exits; a killed
  supervisor leaves the gate armed. Add `supervisor_beat` to fleet_config and require freshness in
  the lease CTE (same pattern as the canary gate) + an out-of-band dead-man alert on stale heartbeats.
- **Score the backlog** (10,629 enriched unscored) on machine 2; restart discovery.
- **Run the res_build bridge** (`bridge-resbuild.ps1 -DryRun` → `-Limit 15`): 206 offsite kept jobs
  staged, 134 below-threshold unlocked. Still 0 promoted.
- **LinkedIn decision point**: canary_remaining=0 freezes 1,255 queued jobs after only 2 canary
  applies. Review those 2, then refill a small daily allotment per the runbook — or disable cleanly.
- **Repo health**: 11 commits on NO remote (push after coordinating with the other session);
  3 red tests (`test_preference_profile_scoring.py` stale FakeClient stubs); TS typecheck broken
  10 days (`adjudicatedScoring.ts:425`, `importJobs.ts:184`); `searches_tuned.yaml` load-order bug
  still live in config.py (any invocation not via run-applypilot.ps1 silently uses wrong searches).
- **Dynamic IP rot**: 192.168.1.187 baked into enrollment docs, per-box persisted env, pgpass,
  codex config — switch everything to Tailscale 100.90.104.99 (or MagicDNS name).
- **Fleet PG has ZERO backup** while it exclusively holds applied_set (the double-apply guard,
  1,101 rows), 75 applies, and all challenge state. Schedule pg_dump nightly. Also decouple brain
  backups from apply activity (currently only backs up while the supervisor runs).

## 🟡 Medium / structural

- **Email→job matching provably misattributes** (2/26 matched rejections predate the application);
  this same data feeds the crash_unconfirmed→applied reconciler — tighten matching before the next
  reconcile pass.
- **Outcome loop can't learn**: classifier only emits acknowledged/rejected (no interview/recruiter
  stage), and 86.6% of applies sit in the 9.8–10.0 score band — no score→outcome gradient is
  learnable. Add interview stages + consider stratified sampling below the top band.
- **653 gate-passing offsite jobs excluded as auth-gated ATS** — concentrated at marquee employers
  (Workday tenants: RBC, Adobe, FIS, BMO, CIBC, TD…). A supervised home-lane mode (one-time account
  per tenant) is the second-biggest volume unlock after the scheduler.
- **158 open auth challenges / 136 jobs parked to 2036** — nothing surfaces them to the owner;
  build the triage pass the console promises.
- **OneDrive risk repeat**: 7.2GB volatile data (incl. 420MB live queue JSON rewritten in place)
  inside the OneDrive-synced res_build repo — the exact corruption mode that bit the brain. Move
  data/ out (junction) + archive ~2.3GB of dead .bak weight.
- Code hygiene: scorer's blanket `except` turns programming errors into silent score=0 runs;
  `FLEET_PG_DSN` not read by pgqueue.connect() (env-var split); zombie `-NoExit` worker windows +
  orphaned Chromium on kill paths; bare `applypilot run` still spends LLM on tailor/cover/pdf under
  base-resume policy; launcher.py 2,613 / cli.py 2,324 lines with a hand-synced duplicated ORDER BY;
  console :8787 has no auth token (any LAN device can un-pause lanes); pgpass readable by
  CodexSandboxUsers ACL; two fighting writers on fleet_desired_state (fleet.ps1 vs RAM balancer).

## Known-good (verified, no action)

Dedup is working (~7–13% redundancy among gate-passers); PG-side applied_set held for all 75
unsynced applies (never-phantom-apply invariant intact); brain has integrity-gated local backups;
console has LAN bind safety; the 483 crash_unconfirmed quarantine is the safety design working
(445 = the known Codex no_result_line parse-artifact class).

## Explicit unknowns (not audited — carry, don't assume clean)

m2/m4 remote box state; LinkedIn account standing after the 0.465 home-IP challenge rate;
actual DeepSeek dashboard spend vs the $140.62 estimate; QA of a sampled apply transcript.

*Full evidence (queries, file:line quotes, verifier notes) in the workflow output:
session scratchpad `tasks/wb3m8hoom.output`.*
