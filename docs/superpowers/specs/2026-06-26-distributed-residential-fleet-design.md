# Distributed Residential ApplyPilot Fleet — Design Spec

**Status:** Proposed (canonical design doc, for owner review)
**Date:** 2026-06-26
**Builds on:** shelved Railway datacenter fleet (`src/applypilot/apply/{fleet_schema.sql,pgqueue.py,fleet_sync.py,container_worker.py,launcher.py}`), unified-brain pipeline spec, authoritative SQLite brain (`database.py`).
**Principle:** Jonathan applies AS HIMSELF, with his own resume, ONLY to jobs his system already scored qualified. Friend machines hold application DATA only — never passwords.

---

## 1. Architecture Overview + Topology

The system has three planes that share **one Postgres coordination layer** and write back to **one authoritative SQLite brain**:

- **Brain plane (owner's home box):** SQLite at `%LOCALAPPDATA%/ApplyPilot/applypilot.db`, ~77k jobs, Python-owned schema. The only durable store. Pushes work to Postgres, pulls results back.
- **Compute plane (cloud + residential, any IP):** discover / enrich / score / audit / triage / tailor. No captcha, no IP sensitivity → free horizontal parallelism. Reads job rows from a compute queue, writes advisory results back to the brain.
- **Apply plane:** split into two lanes — **no-login ATS** (Greenhouse / Lever / Ashby form-fills, distributed across ~10–12 residential machines) and **LinkedIn / login** (one stable owner machine + IP only).

```
                         ┌────────────────────────────────────────────┐
                         │   OWNER HOME BOX (authoritative)            │
                         │   SQLite brain  applypilot.db (~77k jobs)  │
                         │   Python: schema owner + fleet_sync bridge │
                         │   LinkedIn worker (ONE IP, login lane)     │
                         └───────────────┬────────────────────────────┘
                            PUSH ▲       │ PULL (results → brain)
                        (work)   │       ▼
                  ┌──────────────┴───────────────────────────────┐
                  │      POSTGRES (coordination only, transient)  │
                  │  compute_queue | apply_queue | linkedin_queue │
                  │  rate_governor | results sink | challenge box │
                  │  global daily caps + per-host gap + leases    │
                  └───┬───────────────┬───────────────────────┬──┘
       lease (no-login ATS)      lease (compute)        lease (LinkedIn)
                  │               │                          │
      ┌───────────▼──┐  ┌─────────▼─────────┐      (owner box only —
      │ RESIDENTIAL  │  │ CLOUD COMPUTE      │       linkedin_queue is
      │ APPLY WORKERS│  │ (proxied/any IP)   │       single-consumer)
      │ machines     │  │ score/audit/tailor │
      │ #1..#12      │  │ stateless          │
      │ (friends +   │  └────────────────────┘
      │  owner)      │  ┌────────────────────┐
      │ fill-as-Jon  │  │ RESIDENTIAL COMPUTE│
      │ captcha→human│  │ (same machines,    │
      └──────────────┘  │  idle CPU)         │
                        └────────────────────┘
```

**Key boundaries.** Compute may run anywhere (datacenter IPs are fine — no anti-bot wall). No-login ATS applies MUST run on residential IPs (datacenter failed 0/18 on captcha). LinkedIn MUST run on exactly one machine/IP (one account from many IPs = takeover ban).

---

## 2. Source of Truth + Sync Model

**SQLite brain is authoritative and permanent.** Postgres is a transient coordination bus: a work queue, a global rate governor, and a results sink. Nothing lives in Postgres beyond a job's in-flight lifecycle; if Postgres were wiped, the brain reconstructs every queue from scratch.

**Sync directions (exactly two, both idempotent, keyed on `url`):**

- **PUSH (brain → Postgres):** `fleet_sync.push_offsite_jobs()` selects eligible rows (`COALESCE(audit_score, fit_score) >= 7`, `liveness_status != 'dead'`, not applied/in-flight, `application_url LIKE 'http%'`, LinkedIn excluded) and UPSERTs ~6 routing columns into `apply_queue`. A parallel `push_compute_jobs()` fills `compute_queue` with rows needing scoring/audit/tailor. A parallel `push_linkedin_jobs()` fills `linkedin_queue` (LinkedIn targets only). UPSERT only touches `queued` rows, so re-push never disturbs in-flight work.
- **PULL (Postgres → brain):** `fleet_sync.pull_results()` reads terminal rows (`synced_to_home_at IS NULL`), writes them into the brain (`_PULL_APPLIED` / `_PULL_TERMINAL`, never demoting a confirmed apply), commits, then stamps `mark_synced`. A crash between write and stamp just re-pulls — replay is a no-op behind the `apply_status != 'applied'` guard.

**Cadence.** PUSH on a 5-min loop (or on-demand after a scoring batch). PULL on a 60-sec loop while any worker is active. Compute results PULL on the same 60-sec loop.

**Reconciling the single-owner DB service.** The lock-contention audit's recommendation stands, but at the *right layer*: SQLite writes are serialized **at the home process** (WAL mode + `busy_timeout=60s` + thread-local connections), which is the only writer. Postgres needs no serialization — `FOR UPDATE SKIP LOCKED` gives N workers distinct rows lock-free. So: **Postgres = distributed coordination; SQLite = authoritative local store; `fleet_sync` = the bridge.** When research+apply eventually run 24/7, wrap the brain in a localhost single-owner service (unified-brain Option C); until then the on-demand bridge is sufficient. No friend machine ever touches SQLite.

---

## 3. Postgres Schema

Reuse `apply_queue` (lease model, status enum, reclaim indexes) verbatim and add residential columns + three new tables.

```sql
-- Reused, with residential additions:
ALTER TABLE apply_queue ADD COLUMN worker_home_ip   TEXT;     -- sending residential IP
ALTER TABLE apply_queue ADD COLUMN target_host      TEXT;     -- effective apply host
ALTER TABLE apply_queue ADD COLUMN lane             TEXT DEFAULT 'ats';  -- 'ats'
-- est_cost_usd stays but is 0 for residential (no cloud charge).

-- Compute queue (scoring/audit/tailor; any IP):
CREATE TABLE compute_queue (
  url          TEXT PRIMARY KEY,
  task         TEXT NOT NULL,            -- 'score' | 'audit' | 'tailor' | 'enrich'
  payload      JSONB,                    -- job text / context needed for the task
  status       apply_queue_status NOT NULL DEFAULT 'queued',
  lease_owner  TEXT,
  lease_expires_at TIMESTAMPTZ,
  attempts     INTEGER DEFAULT 0,
  result       JSONB,                    -- advisory score/audit/tailored-resume ref
  synced_to_home_at TIMESTAMPTZ,
  updated_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_compute_lease ON compute_queue (status) WHERE status='queued';

-- LinkedIn queue (single-consumer; owner machine only):
CREATE TABLE linkedin_queue (LIKE apply_queue INCLUDING ALL);  -- same shape, separate lane

-- GLOBAL rate governor (fleet-wide counters; the heart of N-machine safety):
CREATE TABLE rate_governor (
  scope_key      TEXT PRIMARY KEY,       -- 'host:boards.greenhouse.io' | 'global' | 'home_ip:1.2.3.4'
  window_start   TIMESTAMPTZ NOT NULL,   -- rolling 24h anchor
  count_24h      INTEGER NOT NULL DEFAULT 0,
  daily_cap      INTEGER NOT NULL,       -- per-host/global/per-home cap
  last_applied_at TIMESTAMPTZ,           -- for per-host min-gap
  min_gap_seconds INTEGER NOT NULL DEFAULT 90,
  updated_at     TIMESTAMPTZ DEFAULT now()
);

-- Human-in-the-loop challenge box:
CREATE TABLE auth_challenge (
  url          TEXT PRIMARY KEY,
  worker_id    TEXT,                     -- machine that hit the wall
  kind         TEXT,                     -- 'visible_captcha' | 'sms_otp' | 'login_gate'
  screenshot   BYTEA,                    -- what the human needs to see
  raised_at    TIMESTAMPTZ DEFAULT now(),
  resolved_at  TIMESTAMPTZ,
  outcome      TEXT                      -- 'solved' | 'skipped' | 'deferred'
);
```

**Atomic claim with expiry (governor-aware).** Extend the existing `lease_one` so the lease is *refused* when any global counter is over cap. A single statement leases the highest-scored eligible job whose host is (a) past its min-gap and (b) under both its per-host and the global daily cap:

```sql
WITH gov AS (
  SELECT scope_key, count_24h, daily_cap, last_applied_at, min_gap_seconds
  FROM rate_governor
),
global_ok AS (
  SELECT (count_24h < daily_cap) AS ok FROM gov WHERE scope_key = 'global'
),
next_job AS (
  SELECT q.url, q.target_host
  FROM apply_queue q
  JOIN gov g ON g.scope_key = 'host:' || q.target_host
  CROSS JOIN global_ok
  WHERE q.status = 'queued'
    AND q.lane = 'ats'
    AND global_ok.ok
    AND g.count_24h < g.daily_cap
    AND (g.last_applied_at IS NULL
         OR g.last_applied_at < now()
            - make_interval(secs => g.min_gap_seconds * (0.7 + random()*0.7)))
  ORDER BY q.score DESC, q.url
  LIMIT 1
  FOR UPDATE OF q SKIP LOCKED
)
UPDATE apply_queue q
SET status='leased', lease_owner=%(worker)s,
    lease_expires_at = now() + make_interval(secs => 1200),
    last_attempted_at = now(), attempts = q.attempts + 1, updated_at = now(),
    worker_home_ip = %(home_ip)s
FROM next_job WHERE q.url = next_job.url
RETURNING q.url, q.company, q.title, q.application_url, q.target_host, q.score;
```

The counter increment happens at **result-write** time (success only) inside the same transaction as `write_result`, so the governor reflects actual sends. **Reclaim** (unchanged): a sweep flips `leased` rows whose `lease_expires_at < now()` back to `queued` — a dead/offline worker's job re-queues automatically (TTL 1200s > 900s job timeout + grace).

---

## 4. Lane Split Enforcement

Three mechanisms, defense-in-depth:

1. **Queue partitioning.** LinkedIn targets go to `linkedin_queue` (never `apply_queue`). `push_offsite_jobs` already filters `application_url NOT LIKE '%linkedin.com%'`; a symmetric `push_linkedin_jobs` routes the inverse. No-login ATS lives only in `apply_queue` (`lane='ats'`).
2. **Worker capability flags.** Each worker registers a capability set when it connects: residential apply workers advertise `{can_ats: true, can_linkedin: false}`; the owner machine advertises `{can_linkedin: true}`. The lease RPC filters by lane the worker is allowed to claim. `linkedin_queue` is single-consumer: only the owner's `worker_id` may lease from it (enforced by a row policy / a `WHERE lease_owner_allowed = owner_id`).
3. **Server-side guard.** A Postgres trigger rejects any `apply_queue` insert whose `target_host` matches `linkedin.com`, and rejects any `linkedin_queue` lease whose `worker_id != <owner>`. Even a misconfigured worker cannot cross lanes.

---

## 5. Worker Model (Residential)

Each residential machine runs a **thin Python worker** (reuse `container_worker.py`'s loop, swap env):

**Loop:** `lease_one()` (ATS lane, governor-gated) → hydrate Jonathan's data → drive a real browser (Playwright + a persistent residential Chrome profile) to fill the no-login form as Jonathan → detect captcha/auth wall → if hit, raise an `auth_challenge` and defer; else submit, verify confirmation, `write_result()` (increments governor counter on success).

**What it holds (data minimization):** `profile.json` (name, email, phone, work history, links) + `resume.pdf`, hydrated from Postgres `fleet_assets` (or pushed once at install). **No passwords, no LinkedIn cookies, no SQLite brain, no broad DB creds.** Assets live in the worker's local `%LOCALAPPDATA%/ApplyPilot/` (Win) or `~/Library/Application Support/ApplyPilot/` (Mac) and are deletable on revocation.

**PG auth (no broad creds — see §9):** the worker authenticates to a **thin API broker**, not to Postgres directly. It holds a per-machine bearer token; the broker exposes only `lease`, `write_result`, `raise_challenge`, `heartbeat`, and `fetch_assets`. Friends never get a Postgres DSN.

**Offline / resume behavior.** No heartbeat for > lease TTL → the reclaim sweep re-queues the job; another machine picks it up. On restart, the worker simply resumes leasing. In-flight work that crashed mid-submit lands as `crash_unconfirmed` (pinned, never blind-retried — protects against double-apply).

**Install footprint.** One installer per OS: Python 3.12 runtime (embedded) + Playwright Chromium + the worker package + a tray app. Win: signed `.exe` / scheduled task; Mac: signed `.pkg` / `launchd` agent. Tray shows status (idle / applying / **needs you** for a challenge) and a one-click **Pause** and **Uninstall+wipe**.

---

## 6. Global Rate Governor

The governor turns N independent machines into one polite client. Three caps, all enforced **inside the atomic claim** (§3) so they're true fleet-wide, not per-process:

- **Per-host min-gap + jitter:** `rate_governor.last_applied_at` per `host:<domain>`, with `min_gap_seconds * (0.7 + random()*0.7)`. A worker cannot lease a job for a host another worker just hit — the gap is global because the timestamp lives in shared Postgres, not in any machine's memory.
- **Per-host daily cap:** `count_24h < daily_cap` per host (e.g. Greenhouse 60/day, Lever 50/day fleet-wide). Tunable per host as anti-bot tolerance is learned.
- **Global daily cap:** `scope:'global'` row caps total fleet applies/day (e.g. 200). Replaces the datacenter `$200 spend cap` (residential cost is $0).
- **Optional per-home cap:** `home_ip:<ip>` row limits any single residential IP, so one friend's line isn't overrepresented to a host.

Counters increment atomically with `write_result` on success only; a nightly job rolls `window_start` and resets `count_24h`. Because the cap check and the lease are one statement under `SKIP LOCKED`, two workers cannot both slip past the last unit of a cap — the row lock serializes the decrement-of-headroom. **LinkedIn's rolling-24h cap (~20/day) stays home-side** in `launcher.py` — it's single-machine and never enters the fleet governor.

---

## 7. Captcha / Human-in-the-Loop

Residential IPs pass invisible reCAPTCHA v3 far better than datacenter, so most applies never see a wall. When one does:

- **Detection:** the worker recognizes a visible captcha / SMS-OTP / unexpected login gate, **does not** guess. It screenshots the page, writes an `auth_challenge` row (`kind`, `screenshot`, `worker_id`), and sets the job to a `challenge_pending` lease-hold (lease frozen, not released, so no other machine grabs it).
- **UX:** the local tray app raises a **"ApplyPilot needs you"** notification on *that* machine. Clicking opens the already-loaded browser window where the human solves the captcha / enters the code, then clicks **Done**. The worker verifies submission and writes the result; the challenge row is stamped `solved`.
- **No human present:** after a **challenge timeout** (e.g. 10 min) the worker marks the challenge `deferred`, releases the lease, and the job re-queues with a `defer_until = now()+6h` backoff. After K defers it's marked `failed:human_unavailable` and surfaced in the owner's review UI for a manual decision. Challenges are **never** auto-routed to a different friend's machine (the captcha is bound to that browser session/IP).

---

## 8. Compute Distribution

Scoring / audit / triage / tailor have **no captcha and no IP sensitivity**, so they ride the same lease pattern on `compute_queue` across cloud + residential idle CPU:

- **PUSH:** `push_compute_jobs()` enqueues rows needing a compute task (`task='score'|'audit'|'tailor'|'enrich'`) with the minimal `payload` JSONB they need.
- **Lease:** identical `FOR UPDATE SKIP LOCKED` claim (no governor gate — nothing to throttle). Cloud workers (proxied, any IP) and residential idle workers compete freely.
- **Write-back:** result JSONB lands in `compute_queue.result`; `pull_results()` ingests it into the brain as **advisory** rows (`research_fit_score`, `research_decision`, tailored-resume refs) — never auto-promoted to `fit_score`/`audit_score`; the owner promotes explicitly (unified-brain rule). Tailored resumes are stored as assets and referenced by URL, respecting the "apply as-is unless owner opts in" rule.

This is the cheapest stage to scale first (S1) because it's the lowest-risk — no account, no IP, no human.

---

## 9. Consent + Data + Security

- **What a friend installs:** one signed installer + a tray app. Nothing else. Clear consent screen at install: *"This runs job applications as Jonathan from this machine. It stores his resume/contact info locally and no passwords. You can pause or fully remove it anytime."*
- **What a friend holds:** `profile.json` + `resume.pdf` only, local, encrypted at rest (OS keychain-wrapped key). **No passwords, no LinkedIn, no brain, no other people's data.**
- **What a friend sees:** tray status + challenge prompts for jobs running on their machine. They do **not** see the brain, other machines, or aggregate data.
- **Securing the PG connection:** friends get a **per-machine bearer token to the API broker**, never a Postgres DSN. The broker holds the only DB credential, exposes a tiny RPC surface (lease/result/challenge/heartbeat/assets), and rate-limits/audits per token. This means a leaked token can't dump the queue or touch the brain — and it's revocable in one row.
- **Revocation / kill-switch:** (a) `fleet_config.paused = true` halts all leasing instantly (global kill). (b) Revoking a machine's broker token stops that machine alone. (c) Tray **Uninstall+wipe** removes assets and the worker. (d) Per-machine token rotation on a schedule. Every apply is logged with `worker_id` + `machine_owner` for a consent audit trail.

---

## 10. Staged Rollout

Each stage is independently shippable and reversible.

- **S1 — Distribute compute only (zero apply risk).** Add `compute_queue`, `push/pull` for it, and the broker. Run compute workers on the owner box + 1 cloud node. Validate lease/reclaim/write-back against the brain. *Ship gate:* advisory scores flow back, no brain corruption.
- **S2 — One residential apply worker (owner's own second machine).** Add governor tables + governor-gated claim + `auth_challenge` + tray app. Run a tiny daily cap (e.g. global 10/day). *Ship gate:* applies succeed on residential IP, captchas route to the tray, governor caps hold.
- **S3 — Add 1–2 friend machines, then scale to ~10–12.** Per-machine tokens, consent flow, per-home caps. Raise global cap gradually while watching per-host failure rates. *Ship gate:* no host shows elevated block rate; consent/audit logging complete.
- **S4 — Cloud compute scale-out.** Add proxied cloud compute nodes to `compute_queue` for discovery/score/audit/tailor throughput. *Ship gate:* compute throughput up, apply lanes unaffected.
- **S5 — LinkedIn lane (last, owner-only).** Wire `linkedin_queue` single-consumer on the owner box, keep the home-side rolling-24h cap. *Ship gate:* LinkedIn applies only ever originate from the one IP.

---

## 11. Risks + Open Questions

- **Double-apply across machines.** Mitigated by posting-level dedup in PUSH + `crash_unconfirmed` pinning, but a confirmed-but-unsynced apply could in theory be re-leased after a long Postgres/home partition. *Open:* add a pre-submit "already-applied?" check against a fast Postgres `applied_urls` set.
- **Governor accuracy under clock skew / reclaim.** Counters increment on success only; a job that succeeds but whose `write_result` is lost (worker dies post-submit) under-counts the governor. Acceptable (errs toward politeness) but worth monitoring.
- **Friend-machine trust.** A malicious friend could tamper with the local worker. Data minimization caps the blast radius (resume + contact only), but *open:* should applies from a machine be signed/attested?
- **Broker as single point of failure.** It's the only path to PG; needs HA or at least fast restart. *Open:* run it on the owner box or a managed host?
- **Captcha binding.** Assumed captcha is bound to the browser session/IP that raised it (hence no cross-machine routing). *Open:* verify per-ATS whether a challenge can be solved out-of-band.
- **Per-host caps are guesses.** Initial Greenhouse/Lever/Ashby daily caps are unvalidated. *Open:* instrument block-rate per host and auto-tune `daily_cap`/`min_gap_seconds`.
- **Residential ToS / consent durability.** Friends may revoke informally (turn the machine off). The defer/timeout path handles it, but *open:* define a clean "machine retired" state that drains its in-flight leases.
```
