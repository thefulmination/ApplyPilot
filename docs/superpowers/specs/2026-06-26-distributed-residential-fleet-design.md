# Distributed Residential ApplyPilot Fleet — Design Spec (v2)

**Status:** Proposed (canonical design doc, for owner review). **v2 — folds in LinkedIn-N-machines, cloud-brain optionality, captcha engine+resilience, Gmail relay, Playwright scaling, fleet health/recovery, outcome tracking, dedup, answer-bank, approval gate, auto-update, stewardship, cost governor, canary.**
**Date:** 2026-06-26 (v2)
**Builds on:** shelved Railway datacenter fleet (`src/applypilot/apply/{fleet_schema.sql,pgqueue.py,fleet_sync.py,container_worker.py,launcher.py}`), unified-brain pipeline spec, authoritative SQLite brain (`database.py`), `brainDb.ts` (backend-agnostic data layer), `gmail_outcomes` parser + `inbox_events` scanner, existing question-bank and captcha-probe.
**Principle:** Jonathan applies AS HIMSELF, with his own resume, ONLY to jobs his system already scored qualified AND he has cleared. Friend machines hold application DATA only — never passwords, never the Gmail token. The fleet is a politeness-bounded, owner-gated good-faith applicator — not a volume cannon.

---

## 0. What changed in v2 (orientation)

v1 established the three-plane architecture, the SQLite↔Postgres sync bridge, the lane split, the lease-queue worker model, the global rate governor, the captcha human-in-the-loop, compute distribution, consent/security, and the staged rollout. v2 preserves all of that and folds in fifteen owner requirements. The structural deltas (section numbers below are the **final v2 numbers** and are authoritative):

- **The "brain" is now backend-agnostic** behind the `brainDb` data layer, with TWO supported topologies (local SQLite OR cloud Postgres-as-brain). The sync bridge is now topology-dependent — it *disappears* in the cloud-brain topology. (§1.5 new, §2 expanded)
- **The broker grew up.** It is no longer just an RPC shim to Postgres; it is now also the **Gmail auth-code relay**, the **answer-bank server**, the **config/remote-command channel**, the **asset server**, and the **version server**. (§5, §7, §9, §10, §11, §14 expanded)
- **The governor grew up.** It went from counts+caps to **outcome-aware adaptive** with a per-IP and per-host circuit-breaker, auto-demote, and a **cost dimension** (the cost cap itself lives in `fleet_config`, §13). (§6 expanded)
- **The dashboard is new and central.** Single pane of glass surfacing fleet health, the captcha inbox, outcomes/interviews, and the approval gate. (§11 new, plus hooks throughout)
- **New top-level sections:** Deployment Topologies (§1.5), Apply Quality — Dedup + Answer Bank + Approval Gate (§9), Outcome Tracking + Feedback Loop (§10), Fleet Health & Recovery (§11), Cost Governor (§13), Auto-Update / Version Management (§14), Per-Machine Canary (§15), Friend-Machine Stewardship (§16).
- **The LinkedIn lane relaxed** from "exactly one machine" to "ONE IP, N coordinated machines, governor-serialized." (§1, §4, §6 updated; deliberately reverses a v1 statement — flagged in §6 and §18.)

---

## 1. Architecture Overview + Topology

The system has three planes that share **one coordination layer** and write back to **one authoritative brain** (which backend hosts that brain is a deployment choice — see §1.5):

- **Brain plane:** ~77k jobs, Python-owned schema, the only durable store, reached through the backend-agnostic `brainDb` data layer. In the default topology it is SQLite at `%LOCALAPPDATA%/ApplyPilot/applypilot.db` on the owner's home box; in the cloud topology it is Postgres (see §1.5). Pushes work, pulls results — or *is* the queue directly, depending on topology.
- **Compute plane (cloud + residential, any IP):** discover / enrich / score / audit / triage / tailor. No captcha, no IP sensitivity → free horizontal parallelism. Reads job rows from a compute queue, writes advisory results back to the brain. Costs real API money — tracked by the cost governor (§13).
- **Apply plane:** split into two lanes — **no-login ATS** (Greenhouse / Lever / Ashby form-fills, distributed across ~10–12 residential machines) and **LinkedIn / login** (the owner's single public IP, but now allowing N coordinated machines behind that one IP — see R1 / §1 "Key boundaries").

```
                         ┌──────────────────────────────────────────────────┐
                         │   OWNER HOME BOX (authoritative in local topo)    │
                         │   brainDb → SQLite applypilot.db (~77k jobs)      │
                         │   Python: schema owner + fleet_sync bridge        │
                         │   LinkedIn workers: ONE public IP, N machines     │
                         │   (governor-serialized — never 2 concurrent)      │
                         │   Gmail OAuth token + inbox SCANNER live HERE      │
                         │   (or on the broker) — never on a friend box      │
                         └───────────────┬──────────────────────────────────┘
                            PUSH ▲       │ PULL (results + outcomes → brain)
                        (work)   │       ▼          (bridge VANISHES in cloud-brain topo §1.5)
                  ┌──────────────┴────────────────────────────────────────┐
                  │   POSTGRES (coordination; OR the brain itself §1.5)    │
                  │  compute_queue | apply_queue | linkedin_queue          │
                  │  rate_governor (outcome-aware, adaptive)               │
                  │  results sink | auth_challenge / CAPTCHA INBOX         │
                  │  applied_set (dedup) | answer_bank | inbox_events      │
                  │  worker_heartbeat | poison_jobs | fleet_config         │
                  │  llm_usage (cost) | remote_commands | otp_request      │
                  └───┬───────────┬────────────────┬─────────────┬────────┘
                      │           │                │             │
            lease     │   lease   │      lease      │   serves    │ reads (single pane)
          (no-login)  │ (compute) │   (LinkedIn)    │ (RPC/relay) │
                      │           │                │             │
   ┌──────────────────▼┐ ┌────────▼────────┐ ┌─────▼──────┐  ┌──▼──────────────────┐
   │ RESIDENTIAL APPLY │ │ CLOUD COMPUTE   │ │ LinkedIn   │  │  THIN API BROKER     │
   │ WORKERS #1..#12   │ │ (proxied/any IP)│ │ workers    │  │  • lease/result RPC  │
   │ (friends + owner) │ │ score/audit/    │ │ behind the │  │  • Gmail code RELAY  │
   │ WORKERS=N each    │ │ tailor stateless│ │ ONE owner  │  │  • ANSWER-BANK serve │
   │ fill-as-Jon       │ │ heartbeat+cost  │ │ IP (gov-   │  │  • asset server      │
   │ heartbeat→broker  │ └────────┬────────┘ │ serialized)│  │  • config + remote   │
   │ captcha→OWNER inbox│ ┌───────▼─────────┐ └────────────┘  │    commands          │
   │ watchdog + canary │ │ RESIDENTIAL CMP │                  │  • version server    │
   └────────┬──────────┘ │ (idle CPU)      │                  │  • holds the ONLY DB │
            │            │ heartbeat+cost  │                  │    cred + Gmail token│
            │            └───────┬─────────┘                  │  • inbox SCANNER →   │
            │ heartbeat /        │ heartbeat / cost           │    inbox_events      │
            │ outcomes / health  │                            └──────────┬───────────┘
            ▼                    ▼                                       │ reads
   ┌──────────────────────────────────────────────────────────────────────▼─────────┐
   │  CENTRALIZED DASHBOARD (owner box or broker; reads PG) — single pane of glass    │
   │  fleet health • per-IP captcha/block + throttle/demote • queue vs caps •         │
   │  CAPTCHA INBOX • INTERVIEW REQUESTS (loud) • approval gate • quarantine • cost    │
   │  ACTIONS: restart worker • pause machine • clear captcha • approve batch • review │
   └─────────────────────────────────────────────────────────────────────────────────┘
```

**Key boundaries.**
- Compute may run anywhere (datacenter IPs are fine — no anti-bot wall).
- No-login ATS applies MUST run on residential IPs (datacenter failed 0/18 on captcha).
- **LinkedIn: ONE IP, N coordinated machines (R1).** v1 said "exactly one machine." We relax this: the takeover-ban risk is driven by **one account being driven from many *public IPs***, not many *processes*. Two (or more) machines in the owner's house share **one public IP**, so the many-IPs ban risk does **not** apply to them. The governor **SERIALIZES** these machines — never two concurrent automated sessions on the account at once — and the **COMBINED** rate stays under the ~20/day per-account cap. `linkedin_queue` therefore allows **multiple consumers, but only from the single owner IP**, governor-serialized. This is **redundancy and flexibility** (the owner's laptop can pick up if the tower is busy), **not extra throughput** — the per-account cap and serialization are unchanged. A machine on a *different* public IP is still forbidden from the LinkedIn lane.

---

## 1.5. Deployment Topologies (new — R2)

All brain access goes behind the **backend-agnostic `brainDb` data layer**. The rest of the system (queues, governor, workers, broker, dashboard) talks to `brainDb` and is **backend-agnostic**. There are **two supported topologies**, chosen by config (`brain.backend = 'sqlite' | 'postgres'`). Start local-first; flip to cloud when the fleet's scale justifies it.

### Topology A — LOCAL SQLITE (default, home-authoritative)
This is exactly the v1 architecture. SQLite on the owner's home box is the durable brain; Postgres is a transient coordination bus; `fleet_sync` is the bridge between them.
- **Pros:** minimal change from today, data lives on the owner's disk (full control, nothing in the cloud), simplest security story.
- **Cons:** the home box must be up for PUSH/PULL; SQLite is more corruption-prone than a managed PG; the sync bridge is real moving parts.
- **When:** small fleet, owner present, privacy-maximalist. **This is the default.**

### Topology B — CLOUD POSTGRES-AS-BRAIN (always-on, home-independent)
Postgres **is** the brain *and* the queue *and* the governor in one always-on, network-reachable store. `brainDb` simply targets Postgres.
- **Critically: the entire SQLite↔Postgres sync bridge DISAPPEARS.** There is no PUSH, no PULL, no `synced_to_home_at`, no `mark_synced`, no replay-after-crash reconciliation. The queues are *views/derived tables over the brain itself* — eligibility is a `WHERE` clause, not a copied row set. §2's two sync directions collapse to zero. (`fleet_sync.py` becomes a no-op shim under this backend.)
- **Pros:** always-on (no dependence on the home box being awake), network-reachable from every node, **more corruption-robust** than local SQLite (managed PG, WAL, backups, PITR), one fewer subsystem (no bridge).
- **Cons of going cloud:** a one-time **migration of the schema + 77k jobs** into PG; a hard **network dependency** (no LAN-only fallback); **data now lives in the cloud** (privacy/cost tradeoff); `brainDb` must target PG and the Python schema-owner must run against PG. LLM-spend and apply-rate caps gain a network round-trip (negligible).
- **When:** the fleet is large/always-on enough that home-box availability and SQLite robustness become the binding constraints. **Flip to cloud when the fleet justifies it.**

### Backend-agnostic contract
Everything downstream of `brainDb` is identical across topologies:
- **Workers never touch the brain directly in *either* topology** — they only ever talk to the **broker** (§5/§9). In Topology B the broker/governor/queue tables *are* brain tables (same PG instance), so "the broker writes to PG" is the same as "the broker writes to the brain"; the worker still only sees the broker's RPC surface and never holds a brain/PG credential.
- The governor, dedup set, answer bank, outcomes, heartbeats, and dashboard read from the same logical tables; in Topology A they live in the coordination PG, in Topology B they live in the brain PG (same instance). The CREATE TABLE sketches in §3 are written so they're valid in **both** (in Topology A they are coordination tables; in Topology B they are brain tables — the SQL is identical).
- Switching topologies is a config flip + (for A→B) a one-time migration; no worker/broker/dashboard code changes.

---

## 2. Source of Truth + Sync Model

**(Topology A — local SQLite.)** SQLite brain is authoritative and permanent. Postgres is a transient coordination bus: a work queue, a global rate governor, a results sink, and (new in v2) the home for outcomes/dedup/answer-bank/heartbeat/health tables. Nothing lives in coordination-PG beyond a job's in-flight lifecycle and the rolling operational state; if Postgres were wiped, the brain reconstructs every queue from scratch (operational state like heartbeats/outcomes re-accrues).

**Sync directions (exactly two, both idempotent, keyed on `url`):**

- **PUSH (brain → Postgres):** `fleet_sync.push_offsite_jobs()` selects eligible rows (`COALESCE(audit_score, fit_score) >= 7`, `liveness_status != 'dead'`, not applied/in-flight, `application_url LIKE 'http%'`, LinkedIn excluded, **AND owner-approved per the approval gate §9.3** — see the rollout note below). It computes `dedup_key = (company, normalized_role)` (§9.1) and UPSERTs ~7 routing columns (incl. `dedup_key`, `approved_batch`) into `apply_queue`. A parallel `push_compute_jobs()` fills `compute_queue` with rows needing scoring/audit/tailor. A parallel `push_linkedin_jobs()` fills `linkedin_queue` (LinkedIn targets only). UPSERT only touches `queued` rows, so re-push never disturbs in-flight work.
  - **Rollout note (resolves the staging conflict).** The `approved_batch IS NOT NULL` predicate is the global rule **once the approval gate ships in S2**. Before S2 (compute-only, S1) the apply-PUSH is simply **not run** — no apply rows exist yet — so the predicate is not a contradiction, it just isn't exercised until `approved_batch` exists.
- **PULL (Postgres → brain):** `fleet_sync.pull_results()` reads terminal rows (`synced_to_home_at IS NULL`), writes them into the brain (`_PULL_APPLIED` / `_PULL_TERMINAL`, never demoting a confirmed apply), commits, then stamps `mark_synced`. **v2 also pulls outcomes** (`inbox_events` → application response, §10) on the same loop. A crash between write and stamp just re-pulls — replay is a no-op behind the `apply_status != 'applied'` guard.

**Cadence.** PUSH on a 5-min loop (or on-demand after a scoring batch / after the owner approves a batch). PULL on a 60-sec loop while any worker is active. Compute results and outcomes PULL on the same 60-sec loop.

**Reconciling the single-owner DB service.** The lock-contention audit's recommendation stands, but at the *right layer*: SQLite writes are serialized **at the home process** (WAL mode + `busy_timeout=60s` + thread-local connections), which is the only writer. Postgres needs no serialization — `FOR UPDATE SKIP LOCKED` gives N workers distinct rows lock-free. So: **Postgres = distributed coordination; SQLite = authoritative local store; `fleet_sync` = the bridge.** When research+apply eventually run 24/7, wrap the brain in a localhost single-owner service (unified-brain Option C); until then the on-demand bridge is sufficient. No friend machine ever touches SQLite.

**(Topology B — cloud Postgres-as-brain.)** This entire section reduces to: *there is no sync.* The eligibility predicate above becomes the `WHERE` clause of the lease query directly against brain tables; `synced_to_home_at`/`mark_synced` are unused; outcomes write straight into the brain PG (by the broker/scanner, not the worker — §1.5). `dedup_key` is computed at the moment a job becomes apply-eligible (a generated/trigger-maintained column on the brain's jobs view) rather than in PUSH. See §1.5.

---

## 3. Postgres Schema

Reuse `apply_queue` (lease model, status enum, reclaim indexes) verbatim and add residential columns + the new tables. (In Topology A these are coordination tables; in Topology B they are brain tables — identical SQL.)

```sql
-- Reused, with residential additions:
ALTER TABLE apply_queue ADD COLUMN worker_home_ip   TEXT;     -- sending residential IP
ALTER TABLE apply_queue ADD COLUMN target_host      TEXT;     -- effective apply host
ALTER TABLE apply_queue ADD COLUMN lane             TEXT DEFAULT 'ats';  -- 'ats'
ALTER TABLE apply_queue ADD COLUMN dedup_key        TEXT;     -- (company, normalized_role) §9.1
ALTER TABLE apply_queue ADD COLUMN approved_batch   TEXT;     -- owner approval token §9.3
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
  est_cost_usd NUMERIC DEFAULT 0,        -- expected LLM spend for this task §13
  synced_to_home_at TIMESTAMPTZ,
  updated_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_compute_lease ON compute_queue (status) WHERE status='queued';

-- LinkedIn queue (ONE owner IP; N coordinated machines; governor-serialized — R1):
CREATE TABLE linkedin_queue (LIKE apply_queue INCLUDING ALL);  -- same shape, separate lane
-- Allowed consumers are owner-owned worker_ids whose registered public IP == the owner IP.
-- Serialization is enforced by the 'account:linkedin' rate_governor row (see §6 + claim below).

-- GLOBAL rate governor — now OUTCOME-AWARE + ADAPTIVE (R6). Cost CAP lives in fleet_config (§13);
-- this table carries only apply-rate + outcome state, not a parallel cost mechanism.
CREATE TABLE rate_governor (
  scope_key      TEXT PRIMARY KEY,       -- 'global' | 'host:greenhouse.io' | 'home_ip:1.2.3.4'
                                         -- | 'account:linkedin'  (the LinkedIn mutex, §6/R1)
  window_start   TIMESTAMPTZ NOT NULL,   -- rolling 24h anchor
  count_24h      INTEGER NOT NULL DEFAULT 0,   -- CAP counter: increments on CONFIRMED APPLY only
  daily_cap      INTEGER NOT NULL,       -- per-host/global/per-home/per-account cap
  last_applied_at TIMESTAMPTZ,           -- for per-host/per-account min-gap (LinkedIn mutex)
  min_gap_seconds INTEGER NOT NULL DEFAULT 90,
  -- outcome-aware adaptive fields (R6) — these increment on their OWN terminal class,
  -- NOT only on success (this is the deliberate change from v1's "success-only" counters):
  success_24h    INTEGER NOT NULL DEFAULT 0,   -- ++ on confirmed apply
  captcha_24h    INTEGER NOT NULL DEFAULT 0,   -- ++ on a visible-captcha / OTP wall
  block_24h      INTEGER NOT NULL DEFAULT 0,   -- ++ on a hard block / CF-on-everything
  challenge_rate NUMERIC GENERATED ALWAYS AS   -- leading indicator of flagging
                 (CASE WHEN (success_24h+captcha_24h+block_24h)=0 THEN 0
                  ELSE (captcha_24h+block_24h)::numeric
                       /(success_24h+captcha_24h+block_24h) END) STORED,
  breaker_state  TEXT NOT NULL DEFAULT 'ok',   -- 'ok'|'throttled'|'paused'|'demoted'
  breaker_until  TIMESTAMPTZ,                  -- auto-recover time for throttle/pause
  updated_at     TIMESTAMPTZ DEFAULT now()
);

-- Cost ledger (R14) — fleet-wide LLM spend. The CAP is a single home (fleet_config), not a
-- parallel governor mechanism; §6/§8 only REFERENCE the cap, they don't re-implement it.
CREATE TABLE llm_usage (
  id           BIGSERIAL PRIMARY KEY,
  worker_id    TEXT,                     -- which compute node spent it
  machine_owner TEXT,
  task         TEXT,                     -- 'score'|'audit'|'tailor'|'enrich'
  model        TEXT,
  tokens_in    INTEGER,
  tokens_out   INTEGER,
  cost_usd     NUMERIC NOT NULL,
  ts           TIMESTAMPTZ DEFAULT now()
);
-- spend caps live in fleet_config: cost_cap_daily_usd, cost_cap_total_usd (§13).

-- CAPTCHA INBOX / human-in-the-loop challenge box (now classified + routed — R3).
-- PK is a surrogate id, NOT url: the SAME url can wall on a friend box AND be re-attempted
-- on the owner box, raising a second row — a url PK would collide (C4 fix).
CREATE TABLE auth_challenge (
  id           BIGSERIAL PRIMARY KEY,
  url          TEXT NOT NULL,
  worker_id    TEXT,                     -- machine that hit the wall
  machine_owner TEXT,
  home_ip      TEXT,
  kind         TEXT,                     -- classifier output (R3): 'visible_captcha'
                                         -- |'email_otp'|'sms_otp'|'login_gate'
                                         -- |'invisible_block'|'cf'|'invisible_pass'|'clear'
  route        TEXT,                     -- 'owner_inbox'|'owner_tray'|'skip'|'remote_assist'
  screenshot_url TEXT,                   -- broker-served blob ref (NOT inline BYTEA — E2 fix)
  raised_at    TIMESTAMPTZ DEFAULT now(),
  resolved_at  TIMESTAMPTZ,
  outcome      TEXT                      -- 'solved'|'skipped'|'deferred'|'rerouted_owner'
);
CREATE INDEX idx_challenge_open ON auth_challenge (url) WHERE resolved_at IS NULL;

-- Gmail auth-code relay state (R4) — disambiguate concurrent OTPs.
-- NOTE: the extracted code is NEVER persisted (E1 fix) — it is returned only over the RPC
-- response. This table holds request/consumed bookkeeping ONLY, so no secret lingers in PG.
CREATE TABLE otp_request (
  id           BIGSERIAL PRIMARY KEY,
  worker_id    TEXT,
  url          TEXT,
  sender_hint  TEXT,                     -- ATS / sender domain the worker expects
  requested_at TIMESTAMPTZ DEFAULT now(),
  matched_email_ts TIMESTAMPTZ,          -- timestamp of the email the broker matched
  consumed_at  TIMESTAMPTZ              -- mark-consumed so the same email isn't re-handed
);

-- Cross-board DEDUP / distributed double-apply guard (R9):
CREATE TABLE applied_set (
  dedup_key    TEXT PRIMARY KEY,         -- (company, normalized_role) — board-agnostic
  company      TEXT,
  normalized_role TEXT,
  first_applied_at TIMESTAMPTZ DEFAULT now(),
  applied_url  TEXT,                     -- the one URL we actually applied through
  got_response BOOLEAN DEFAULT false     -- set by §10; surfaces "already heard back" semantics
);

-- Screening-question ANSWER BANK (R10) — served by broker, never guessed locally:
CREATE TABLE answer_bank (
  q_norm       TEXT PRIMARY KEY,         -- normalized question key
  q_raw        TEXT,
  answer       TEXT,                     -- owner-vetted answer
  kind         TEXT,                     -- 'work_auth'|'years_x'|'salary'|'eeo'|'why_us'|...
  status       TEXT DEFAULT 'known',     -- 'known'|'unknown_deferred'
  updated_at   TIMESTAMPTZ DEFAULT now()
);

-- OUTCOME tracking / Gmail feedback loop (R8) — written by the inbox SCANNER on the trusted
-- spot (owner box or broker), NOT by workers:
CREATE TABLE inbox_events (
  id           BIGSERIAL PRIMARY KEY,
  dedup_key    TEXT,                     -- ties response → application (R9 key)
  url          TEXT,
  event_type   TEXT,                     -- 'interview'|'rejection'|'assessment'|'ack'
  sender       TEXT,
  received_at  TIMESTAMPTZ,
  raw_snippet  TEXT,
  surfaced     BOOLEAN DEFAULT false     -- interview requests surfaced loudly on dash
);

-- FLEET HEALTH: heartbeat substrate (R5/R7) — every worker ~every 20s:
CREATE TABLE worker_heartbeat (
  worker_id    TEXT PRIMARY KEY,
  machine_owner TEXT,
  home_ip      TEXT,
  role         TEXT,                     -- 'apply'|'compute'|'both' (compute nodes heartbeat too)
  state        TEXT,                     -- 'idle'|'applying'|'challenge_pending'|'paused'
  current_job  TEXT,                     -- url in flight
  job_started_at TIMESTAMPTZ,            -- for over-max-duration detection
  success_today INTEGER DEFAULT 0,
  captcha_today INTEGER DEFAULT 0,
  block_today   INTEGER DEFAULT 0,
  spend_today_usd NUMERIC DEFAULT 0,     -- compute nodes report rolling LLM spend (§13)
  cpu_pct      NUMERIC,
  ram_pct      NUMERIC,
  browser_count INTEGER,                 -- WORKERS=N concurrency (R5)
  sw_version   TEXT,                     -- reported version (R12)
  last_beat    TIMESTAMPTZ DEFAULT now()
);

-- POISON-JOB quarantine (R7): jobs that crash/hang whoever claims them:
CREATE TABLE poison_jobs (
  url          TEXT PRIMARY KEY,
  crash_count  INTEGER DEFAULT 0,
  last_worker  TEXT,
  quarantined_at TIMESTAMPTZ,
  reason       TEXT,
  reviewed     BOOLEAN DEFAULT false
);

-- Fleet config / remote commands / version pin (R7 remote-restart, R12 auto-update):
CREATE TABLE fleet_config (
  key          TEXT PRIMARY KEY,         -- 'paused'|'pinned_worker_version'|'canary_version'
                                         -- |'canary_worker_id'|'cost_cap_daily_usd'
                                         -- |'cost_cap_total_usd'|'approval_threshold'|...
  value        TEXT
);
CREATE TABLE remote_commands (
  id           BIGSERIAL PRIMARY KEY,
  worker_id    TEXT,                     -- target ('*' = fleet-wide)
  command      TEXT,                     -- 'restart'|'pause'|'resume'|'self_update'|'drain'
  target_version TEXT,                   -- for 'self_update': which version to pull (R12 canary)
  issued_at    TIMESTAMPTZ DEFAULT now(),
  acked_at     TIMESTAMPTZ
);
```

**Atomic claim with expiry (governor-aware + outcome-aware + per-IP breaker + dedup + approval).** Extend the existing `lease_one` so the lease is *refused* when (a) any global counter is over cap, (b) the host's **OR the worker's home-IP's** breaker is throttled/paused/demoted or over cap, (c) the posting is already in the `applied_set`, or (d) the job isn't in an approved batch. A single statement leases the highest-scored eligible job whose host AND home-IP are (i) past the (possibly widened) min-gap, (ii) under both per-scope and the global daily cap, (iii) `breaker_state='ok'`, (iv) not already applied, (v) owner-approved:

```sql
WITH gov AS (
  SELECT scope_key, count_24h, daily_cap, last_applied_at,
         min_gap_seconds, breaker_state
  FROM rate_governor
),
global_ok AS (
  SELECT (count_24h < daily_cap) AS ok FROM gov WHERE scope_key = 'global'
),
-- D2 FIX: the per-IP circuit-breaker is the heart of R6, so it MUST gate the claim.
home_ok AS (
  SELECT (count_24h < daily_cap AND breaker_state = 'ok') AS ok
  FROM gov WHERE scope_key = 'home_ip:' || %(home_ip)s
),
next_job AS (
  SELECT q.url, q.target_host
  FROM apply_queue q
  JOIN gov g ON g.scope_key = 'host:' || q.target_host
  CROSS JOIN global_ok
  CROSS JOIN home_ok
  WHERE q.status = 'queued'
    AND q.lane = 'ats'
    AND global_ok.ok
    AND home_ok.ok                                              -- R6 per-IP cap + breaker
    AND g.count_24h < g.daily_cap
    AND g.breaker_state = 'ok'                                  -- R6 per-host circuit-breaker
    AND q.approved_batch IS NOT NULL                            -- R11 approval gate
    AND NOT EXISTS (SELECT 1 FROM applied_set a                 -- R9 dedup guard
                    WHERE a.dedup_key = q.dedup_key)
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
RETURNING q.url, q.company, q.title, q.application_url, q.target_host, q.score, q.dedup_key;
```

**LinkedIn lease (D1 FIX — the `account:linkedin` mutex made concrete).** LinkedIn has its own claim. Eligible workers are owner-IP machines; serialization is the `account:linkedin` governor row whose `min_gap_seconds` acts as a **mutex** (a held lease stamps `last_applied_at`, so no second owner machine can lease until the gap elapses → **never two concurrent automated sessions**, combined rate under the ~20/day cap):

```sql
WITH acct AS (
  SELECT count_24h, daily_cap, last_applied_at, min_gap_seconds, breaker_state
  FROM rate_governor WHERE scope_key = 'account:linkedin'
),
next_li AS (
  SELECT q.url
  FROM linkedin_queue q CROSS JOIN acct a
  WHERE q.status = 'queued'
    AND a.count_24h < a.daily_cap                               -- ~20/day per-account cap
    AND a.breaker_state = 'ok'
    AND a.approved_batch IS NULL OR q.approved_batch IS NOT NULL  -- owner-gated like ATS
    AND (a.last_applied_at IS NULL                              -- the MUTEX / serializer
         OR a.last_applied_at < now() - make_interval(secs => a.min_gap_seconds))
  ORDER BY q.score DESC, q.url
  LIMIT 1
  FOR UPDATE OF q SKIP LOCKED                                   -- only one worker wins the row
)
UPDATE linkedin_queue q
SET status='leased', lease_owner=%(worker)s,
    lease_expires_at = now() + make_interval(secs => 1200), updated_at = now()
FROM next_li WHERE q.url = next_li.url
RETURNING q.url, q.company, q.title, q.application_url, q.score;
-- The broker additionally refuses this lease unless the caller's registered public IP == owner IP.
-- On confirmed apply, write_result stamps account:linkedin.last_applied_at + count_24h (the cap),
-- which is what re-arms the mutex for the next owner machine.
```

**Counter semantics (C3 — reconciled, authoritative).** Inside the same transaction as `write_result`:
- `count_24h` (the **cap** counter) increments **on confirmed apply only** — it is what the daily caps gate against.
- The **outcome** counters increment on their respective terminal classifications: `success_24h++` on confirmed apply, `captcha_24h++` on a captcha/OTP wall, `block_24h++` on a hard block / CF. These drive `challenge_rate` and the adaptive breaker (§6). This is the deliberate change from v1's "success-only" model: walls and blocks now count (that is the whole point of R6).

On confirmed apply, the same transaction UPSERTs `applied_set(dedup_key)` (§9.1). **Reclaim** (unchanged): a sweep flips `leased` rows whose `lease_expires_at < now()` back to `queued` — a dead/offline worker's job re-queues automatically (TTL 1200s > 900s job timeout + grace). A `challenge_pending` lease is *frozen*, not reclaimable (§7).

---

## 4. Lane Split Enforcement

Three mechanisms, defense-in-depth:

1. **Queue partitioning.** LinkedIn targets go to `linkedin_queue` (never `apply_queue`). `push_offsite_jobs` already filters `application_url NOT LIKE '%linkedin.com%'`; a symmetric `push_linkedin_jobs` routes the inverse. No-login ATS lives only in `apply_queue` (`lane='ats'`).
2. **Worker capability flags.** Each worker registers a capability set when it connects: residential apply workers advertise `{can_ats: true, can_linkedin: false}`; the owner's machines advertise `{can_linkedin: true}` **only if their registered public IP equals the owner IP**. The lease RPC filters by lane the worker is allowed to claim. `linkedin_queue` is now **multi-consumer but single-IP (R1):** any owner-owned worker whose public IP == the owner IP may lease from it, but the **governor serializes** them via the `account:linkedin` mutex (§3 LinkedIn lease, §6) so at most one LinkedIn session is automated at a time.
3. **Server-side guard.** A Postgres trigger rejects any `apply_queue` insert whose `target_host` matches `linkedin.com`, and the broker rejects any `linkedin_queue` lease whose worker's registered public IP `!=` the owner IP. Even a misconfigured worker (or a friend machine on a different IP) cannot cross into the LinkedIn lane.

---

## 5. Worker Model (Residential) + Easy Playwright Scaling (R5)

Each residential machine runs a **thin Python worker** (reuse `container_worker.py`'s loop, swap env). A machine runs **`WORKERS=N` concurrent worker slots**, each its own Chromium instance:

**Per-machine concurrency (R5).** `WORKERS=N` is **auto-sized to the machine's RAM/CPU** by default (friend laptop ≈ 2, owner tower ≈ 8), overridable in the tray / config (and capped by stewardship limits, §16). Each worker slot runs an independent Chromium via Playwright with its own persistent residential Chrome profile. **Scaling is trivial because of the lease queue:** a new worker slot — or a whole new machine — just starts **CLAIMING**; there is **no central reconfig or rebalance**. Horizontal (more machines) and vertical (more browsers per machine) are the *same operation*: "add workers." It **stays safe automatically** — the global governor's per-host min-gap (§6) serializes hits to any one host *regardless of how many browsers exist*, so extra browsers simply let slow browser/captcha-wait time on one host overlap with applies to *different* hosts. More browsers never means more hits/host/sec.

**Loop:** `lease_one()` (ATS lane, governor-gated, dedup-gated, approval-gated) → hydrate Jonathan's data → drive a real browser (Playwright + a persistent residential Chrome profile) to fill the no-login form as Jonathan → for each screening question, **ask the broker's answer bank (§9.2)**; an unknown question DEFERS to the owner rather than guessing → run the **captcha detector/classifier (§7)**; on an email-OTP wall, request the code from the **Gmail relay (§7.4)** and continue; on any human-needed wall, **park and move on** (never block) → else submit, verify confirmation, `write_result()` (increments cap counter + outcome counters, UPSERTs `applied_set`). Every ~20s it emits a **heartbeat (§11)**.

**What it holds (data minimization):** `profile.json` (name, email, phone, work history, links) + `resume.pdf`, served by **the broker's `fetch_assets`** RPC (or pushed once at install). There is no `fleet_assets` Postgres table — assets are **broker-served blobs** (C1 fix), consistent with the "friends never get a Postgres DSN" rule. **No passwords, no LinkedIn cookies, no SQLite brain, no broad DB creds, and crucially NO Gmail OAuth token** (§7.4). Assets live in the worker's local `%LOCALAPPDATA%/ApplyPilot/` (Win) or `~/Library/Application Support/ApplyPilot/` (Mac) and are deletable on revocation.

**PG auth (no broad creds — see §9/§12):** the worker authenticates to a **thin API broker**, not to Postgres directly. It holds a per-machine bearer token; the broker exposes `lease`, `write_result`, `raise_challenge`, `heartbeat`, `fetch_assets`, **`get_answer` (answer bank)**, **`request_otp` (Gmail relay)**, **`get_config` / `poll_commands` (remote control + version)**, and **`report_usage` (cost)**. Friends never get a Postgres DSN.

**Offline / resume behavior (stateless self-resume — R7).** No heartbeat for > lease TTL → the reclaim sweep re-queues the job; another machine picks it up. On restart, the worker simply **resumes leasing — there is no recovery state to rebuild**; the missing-heartbeat lease auto-reclaims. In-flight work that crashed mid-submit lands as `crash_unconfirmed` (pinned, never blind-retried — protects against double-apply, and gated again by the §9.1 dedup check).

**Install footprint.** One installer per OS: Python 3.12 runtime (embedded) + Playwright Chromium + the worker package + a tray app + a **per-machine watchdog (§11)**. Win: signed `.exe` / scheduled task; Mac: signed `.pkg` / `launchd` agent. Tray shows status (idle / applying / **needs you** for an owner-machine challenge), `WORKERS=N` and resource caps (§16), and a one-click **Pause** and **Uninstall+wipe**.

---

## 6. Global Rate Governor — Outcome-Aware & Adaptive (R6, R1)

The governor turns N independent machines into one polite client. It now does **counts + caps + an outcome-aware circuit-breaker**, all enforced **inside the atomic claim** (§3) so they're true fleet-wide, not per-process. (The **cost** cap is a separate concern with a single home in `fleet_config`; the governor only *references* it via the compute-lease gate, §8/§13 — there is no parallel cost mechanism here, avoiding drift — E4 fix.)

**Counts & caps (from v1):**
- **Per-host min-gap + jitter:** `rate_governor.last_applied_at` per `host:<domain>`, with `min_gap_seconds * (0.7 + random()*0.7)`. A worker cannot lease a job for a host another worker just hit — the gap is global because the timestamp lives in shared Postgres, not in any machine's memory. **This is also what makes WORKERS=N safe (R5).**
- **Per-host daily cap:** `count_24h < daily_cap` per host (e.g. Greenhouse 60/day, Lever 50/day fleet-wide). Tunable per host as anti-bot tolerance is learned.
- **Global daily cap:** `scope:'global'` row caps total fleet applies/day (e.g. 200). Replaces the datacenter `$200 spend cap` (residential apply cost is $0; LLM cost is governed separately in §13).
- **Per-home cap:** `home_ip:<ip>` row limits any single residential IP, so one friend's line isn't overrepresented to a host. **(Now enforced in the claim — see D2 fix in §3.)**

**LinkedIn serialization (R1).** LinkedIn is single-account. The governor enforces an `account:linkedin` scope row whose **min-gap acts as a mutex**: a LinkedIn lease takes the row lock and stamps `last_applied_at`, so a second owner-IP machine cannot lease a LinkedIn job until the gap elapses — **never two concurrent automated sessions**, and the combined rate stays under the ~20/day per-account cap regardless of how many owner machines are eligible (concrete SQL in §3). This is what lets v1's "exactly one machine" relax to "one IP, N machines" safely.
> **Changed from v1 (flagged deliberately).** v1 §6 stated LinkedIn's rolling-24h cap "stays home-side and **never enters the fleet governor**." v2 reverses that: R1 needs a **fleet-side mutex**, so LinkedIn now lives in the governor (the `account:linkedin` row) **in addition to** the home-side rolling-24h cap in `launcher.py`, which remains as belt-and-suspenders. This is an intentional design change, not an oversight.

**Outcome-aware adaptive circuit-breaker (R6).** Outcome counters increment atomically with `write_result` (per §3 semantics): `success_24h` (confirmed apply), `captcha_24h` (wall), `block_24h` (hard block), plus the generated `challenge_rate`. **Key insight: a rising per-IP captcha/block rate is a LEADING INDICATOR that the IP is being flagged** — before a hard block. So:
- **Auto-throttle / pause:** when a `home_ip:<ip>` (or `host:<domain>`) `challenge_rate` crosses a threshold, set `breaker_state='throttled'` (widen `min_gap_seconds`, drop `daily_cap`) or `breaker_state='paused'` with a `breaker_until` recovery time, so the IP **cools off and RECOVERS before a hard block**. The claim query refuses leases while `breaker_state != 'ok'` (now true for both the host AND the home-IP row — §3).
- **Auto-demote on hard block / CF-on-everything:** an IP that hits a hard block or CF-walls everything is set `breaker_state='demoted'` → it is removed from the apply role and **kept as COMPUTE-ONLY** (compute has no IP sensitivity), and an alert fires. The machine stays useful; the IP cools off.
- **Learn captcha-heavy hosts:** hosts with persistently high `challenge_rate` are **routed to the owner machine** (where the owner can solve locally) or deprioritized in the ORDER BY, so friend-IP attempts aren't wasted on hosts that always wall.

**Safe per-job handling under a wall (R6).** A job that hits a wall is **parked in a frozen `challenge_pending` lease-hold** (not released, not double-claimable, not lost). The worker **MOVES ON immediately — it never blocks on a human.** Re-attempt is gated by the §9.1 double-apply check. Backoff is **bounded:** `defer → K retries → failed:human_unavailable → owner review`, **never infinite-retry**. **Fail-safe:** if detection is uncertain, **treat it as a wall and PARK — never guess or blind-submit** (a blind submit on a captcha page burns the IP).

**Integrity.** Because the cap/breaker check and the lease are one statement under `SKIP LOCKED`, two workers cannot both slip past the last unit of a cap or sneak past a tripped breaker — the row lock serializes the decrement-of-headroom. A nightly job rolls `window_start`, resets `count_24h` and the outcome counters (`success_24h`/`captcha_24h`/`block_24h`), and clears expired `breaker_until` back to `ok`.

---

## 7. Captcha / Human-in-the-Loop — Detector + Local-Solve Engine (R3, R4)

Residential IPs pass invisible reCAPTCHA v3 far better than datacenter, so most applies never see a wall. v2 promotes the existing captcha-probe to a **first-class detector/classifier + a solve-routing engine**. **There is NO captcha-solving SERVICE (no 2Captcha/CapSolver). All solves are HUMAN, in a real browser.**

### 7.1 Detector / classifier (R3)
The worker classifies the post-submit / mid-form state into one of:
`clear` · `invisible_pass` (v3 passed silently) · `visible_captcha` (image/checkbox) · `email_otp` · `sms_otp` · `login_gate` · `invisible_block` (reCAPTCHA v3 low score, no challenge to solve) · `cf` (Cloudflare wall). It **does not guess**; uncertain → treated as a wall (fail-safe, §6).

### 7.2 Solve routing (R3)
- **`clear` / `invisible_pass`:** bulk proceeds normally.
- **`email_otp`:** **auto-solved via the Gmail relay (§7.4)** — no human needed.
- **`visible_captcha` / `sms_otp` / `login_gate` on a FRIEND machine:** **DEFERRED, not dumped on the friend.** An `auth_challenge` row is raised with `route='owner_inbox'`; the friend is **never nagged** (friends run **no-wall applies only**). The job bounces to the **owner's CAPTCHA INBOX** (on the dashboard, §11); the owner's *own* machine **re-attempts the job on the owner's IP**, and the **owner solves it locally**. The friend machine immediately moves on. (The owner-machine re-attempt raises its own `auth_challenge` row — the surrogate `id` PK means it does not collide with the friend's row — C4.)
- **`visible_captcha` / `sms_otp` / `login_gate` on the OWNER machine:** raise the local tray **"ApplyPilot needs you"** notification; the owner solves in the already-loaded browser and clicks **Done**; result is written; row stamped `solved`.
- **`invisible_block` (v3 low score):** **nothing a human can solve** → `skip`, or retry the posting from a **different machine's IP** (different residential IP may score higher). Never park for a human.
- **`cf`:** treat as a hard block signal; counts toward `block_24h` and may trip the breaker / demote the IP (§6).
- **Optional advanced mode (out-of-band proposal — E3).** **Remote-assist** — the owner remotely drives the friend's *live browser session* to solve in-place (`route='remote_assist'`). It is **off by default and deliberately deferred to a separate proposal** so it doesn't expand the consent/security review scope of this spec; the owner-IP re-attempt above already covers the core case. If enabled it requires the friend's explicit opt-in (§16) and keeps the friend's machine/IP in the session so the captcha binding holds.

### 7.3 Park semantics
On any human-needed wall, the worker screenshots the page (stored as a **broker-served blob**, referenced by `screenshot_url` — E2), writes the `auth_challenge` row (`kind`, `route`, `screenshot_url`, `worker_id`, `home_ip`), and sets the job to a **frozen `challenge_pending` lease-hold** (lease not released → no other machine grabs it). **No human present:** after a challenge timeout the worker marks the challenge `deferred`, releases the lease, and the job re-queues with `defer_until = now()+6h`. After K defers → `failed:human_unavailable`, surfaced in the dashboard for an owner decision. Bounded, never infinite (§6).

### 7.4 Gmail auth-code RELAY (R4)
All machines need email verification codes, but the **Gmail OAuth token (`gmail.readonly` = the WHOLE inbox) is NEVER distributed** to friends' machines. The token lives on **ONE trusted spot** — the owner machine or the broker.
- **Flow:** a worker hitting an `email_otp` wall calls the broker `request_otp(sender_hint, url, ts)`. The broker reads Gmail (reusing the **`gmail_outcomes` parser**), extracts **ONLY the verification code** for the matching ATS/sender from the **last ~60s**, and returns just that code **over the RPC response** — the code is **never persisted** (E1). The worker enters it and proceeds.
- **Disambiguating concurrent codes:** match by **sender + timestamp window**, write an `otp_request` bookkeeping row (`sender_hint`, `matched_email_ts`), and **mark-consumed** (`consumed_at`) so the same email isn't matched for two workers. If multiple workers are mid-OTP, the sender_hint + window + consumed flag keep them separate.
- **Friends get CODES, never email.** The broker returns a bare string over RPC; the inbox itself never leaves the trusted spot, and no code is stored.

---

## 8. Compute Distribution

Scoring / audit / triage / tailor have **no captcha and no IP sensitivity**, so they ride the same lease pattern on `compute_queue` across cloud + residential idle CPU:

- **PUSH:** `push_compute_jobs()` enqueues rows needing a compute task (`task='score'|'audit'|'tailor'|'enrich'`) with the minimal `payload` JSONB they need and an `est_cost_usd`.
- **Lease:** identical `FOR UPDATE SKIP LOCKED` claim (no apply-governor gate — nothing IP-sensitive to throttle) — but **gated by the cost cap (§13):** refused when the rolling daily/total LLM spend in `llm_usage` exceeds `fleet_config.cost_cap_*`. Cloud workers (proxied, any IP) and residential idle workers compete freely. **Compute nodes also heartbeat (§11)** and report `spend_today_usd`, so the dashboard governs and monitors them too.
- **Write-back:** result JSONB lands in `compute_queue.result`; each call writes an `llm_usage` row (§13). `pull_results()` ingests results into the brain as **advisory** rows (`research_fit_score`, `research_decision`, tailored-resume refs) — never auto-promoted to `fit_score`/`audit_score`; the owner promotes explicitly (unified-brain rule). Tailored resumes are stored as assets and referenced by URL, respecting the "apply as-is unless owner opts in" rule.

This is the cheapest stage to scale first (S1) because it's the lowest-risk — no account, no IP, no human. **Compute costs real API money**, so it is the one plane the **cost governor (§13)** bounds.

---

## 9. Apply Quality — Dedup, Answer Bank, Approval Gate (new — R9, R10, R11)

The fleet must apply *well*, not just *much*. Three first-class subsystems, all served/enforced centrally (never decided on a friend's box).

### 9.1 Distributed double-apply + cross-board dedup (R9)
- **Pre-submit guard:** before submitting, the worker does a **fast `applied_set` check** in Postgres against a shared applied-set, so **12 machines + retries + the sync gap can't double-apply.** The lease query already excludes already-applied postings (§3); the pre-submit check is the second line in case a concurrent apply landed during the lease window. On confirmed apply, the same transaction UPSERTs `applied_set`.
- **Posting-level (not URL-level) dedup:** the *same company+role* often appears on LinkedIn **and** Greenhouse **and** the company site. URL-level dedup still sends 3 applications to one employer (reads as spam). So we collapse by **`dedup_key = (company, normalized_role)` across boards** and apply **ONCE**.
- **`normalized_role` definition (D4).** `normalized_role` = lowercase; strip seniority decorations to a canonical level token (`jr|sr|staff|principal|lead`), strip Roman/Arabic level suffixes (`II`, `III`, `2`), strip location/req-id/parenthetical noise, collapse synonyms via the existing role-family map (e.g. "Quantitative Developer" ≈ "Quant Dev"). `company` is canonicalized via the existing company-normalization (legal-suffix strip, known-alias merge). The pair is hashed to `dedup_key`. **Where it's computed:** in PUSH for Topology A; as a generated/trigger-maintained column on the brain's apply-eligible view for Topology B (no PUSH) — see §2.
- **`got_response` semantics (D3 — clarified).** The lease/PUSH dedup guard excludes **any** posting already in `applied_set`, regardless of `got_response`. `got_response` is therefore **not** an additional lease-time filter; it is a **display/feedback flag** (§10) used to (a) mark "we already heard back" in the dashboard, and (b) short-circuit any *manual* re-queue attempt the owner might make on an already-answered posting. The lease query's existing `NOT EXISTS (applied_set)` already prevents re-apply; `got_response` does not re-gate it.

### 9.2 Screening-question ANSWER BANK (R10)
- **Centralized + synced, served by the broker (`get_answer`), never guessed locally.** Reuses the **existing question-bank**. Handles custom ATS questions: *years of X, work authorization, salary expectation, "why us", EEO/demographic*.
- **Fail-safe:** an **UNKNOWN question DEFERS to the owner** — the worker **never guesses**, because a wrong answer to a screening question = **instant auto-reject**. The job parks (like a captcha) with `failed:unknown_question` → owner answers it once → the answer is added to `answer_bank` (`status='known'`) → all future workers get it. The bank thus learns from each deferral.

### 9.3 Apply-APPROVAL GATE (R11)
- The fleet **only applies to jobs the OWNER has CLEARED** — either **above an owner-set threshold** (`fleet_config.approval_threshold`) **OR** an explicit **review/approval batch** the owner releases from the dashboard (§11). It **NEVER auto-blasts everything scored ≥7.**
- Mechanically: PUSH only enqueues rows with `approved_batch IS NOT NULL`; the owner approves a batch in the dashboard, which stamps `approved_batch` on the chosen rows and triggers PUSH. The lease query requires `approved_batch IS NOT NULL` (§3). (See the §2 rollout note: this predicate is the global rule once the gate ships in S2.)
- This **preserves the qualification system and the owner's selectivity** — good-faith applying, not a volume cannon. Compute results stay **advisory**; the apply queue is **owner-gated**.

---

## 10. Outcome Tracking + Feedback Loop (new — R8)

This closes the loop from "applied" to "got a result" — the actual goal.

- **Wire the Gmail scanner into the fleet.** The existing `inbox_events` scanner (read-only `gmail.readonly`) runs **on the trusted spot — the owner box or the broker, the same place the OTP relay lives (§7.4), never a friend box.** It classifies each application **RESPONSE** — *interview request / rejection / online-assessment request / acknowledgement* — and writes an `inbox_events` row keyed by `dedup_key` so it ties back to the exact application. (Workers never run the scanner and never touch the inbox.)
- **Flows back to the brain.** On the 60-sec PULL (Topology A) or directly into the brain PG (Topology B), outcomes land tied to the application, and set `applied_set.got_response = true`.
- **SURFACE INTERVIEW REQUESTS LOUDLY (the win).** Interview-request events are pinned at the top of the dashboard (§11) — **never buried under apply noise.** From there **the human takes over; interviews are never automated.**
- **Never re-apply to a job that already got a response.** The posting is already in `applied_set` (so the §3 dedup guard already blocks re-apply); `got_response=true` additionally flags it as "heard back" for the owner and blocks any manual re-queue (§9.1, D3).
- **Learn what converts → feed scoring.** Aggregate which **sources/roles actually CONVERT** (interview rate by board, by role family, by company tier) and feed that signal back into the **qualification/scoring** stage, so the fleet gets **smarter, not just bigger.** (Advisory into the brain; the owner promotes scoring changes per the unified-brain rule.)

---

## 11. Fleet Health & Recovery + Centralized Dashboard (new — R7)

### 11.1 Heartbeat substrate
Every worker → broker **~every 20s** writes a `worker_heartbeat` row: `worker_id`, `machine_owner`, `role` (apply/compute/both), `state` (idle/applying/challenge-pending/paused), `current_job`, today's success/captcha/block counters, `spend_today_usd` (compute nodes), resource use (`cpu_pct`, `ram_pct`, `browser_count`), and **`sw_version` (R12)**.

### 11.2 Multi-signal STUCK detection (concrete thresholds — C5)
With heartbeat ~20s, job timeout 900s, lease TTL 1200s:
- **No heartbeat** for **> 90s** (≈ 4 missed beats) → dead/hung process or network partition.
- **Heartbeat alive but job over max-duration:** `now() - job_started_at > 600s` (`max < 900s` job timeout, so stuck-detection fires *before* the reclaim sweep and they don't fight) → hung browser.
- **Crash-loop** (≥ 3 restarts within 10 min).
- **Resource-pegged** (cpu or ram sustained ≥ 95% across ≥ 3 consecutive beats).

### 11.3 Recovery
- **Per-machine WATCHDOG** auto-restarts a hung worker: **clean-kill the worker AND its Chromium children, free the CDP port (e.g. 9222), clear stale profile locks, relaunch.** (This automates exactly the orphaned-apply-Chromium / stuck-port-9222 cleanup the owner had to do by hand this session.)
- **REMOTE-RESTART from the dashboard:** the owner triggers a `remote_commands` row (`command='restart'`); the **friend does nothing** — the target machine's watchdog polls (`poll_commands`) and executes it locally.
- **POISON-JOB quarantine:** a job that crashes/hangs whoever claims it past K attempts is moved to `poison_jobs`, **pulled from the pool**, and **flagged for owner review** so it **stops taking out workers.**
- **STATELESS self-resume:** a restarted worker just resumes claiming — **no recovery state to rebuild**; the missing-heartbeat lease auto-reclaims (§5).

### 11.4 CENTRALIZED DASHBOARD (single pane of glass)
Runs on the owner box or the broker, **reads Postgres**. It surfaces:
- **Per-machine** liveness / state / health (from `worker_heartbeat`).
- **Per-IP** captcha/block rates + **throttle/pause/demote** state (from `rate_governor`, §6).
- **Queue depth + applies-today-vs-caps** (apply/compute/LinkedIn).
- **The CAPTCHA INBOX** (`auth_challenge` rows routed `owner_inbox`/`owner_tray`, §7) — the owner clears captchas here.
- **INTERVIEW REQUESTS, loud** (from `inbox_events`, §10) — the win, pinned.
- **The APPROVAL GATE** — review/approve batches (§9.3).
- **The QUARANTINE** (`poison_jobs`) and **failures** (`failed:human_unavailable`, `failed:unknown_question`).
- **Cost** — fleet-wide LLM spend vs cap (§13).

**ACTIONS:** restart a worker, pause a machine, clear captchas, approve a batch, review failures, demote/restore an IP, push a **canary / fleet version (§14)**.

**ALERTS:** machine stuck, IP demoted, captcha-backlog spike, throughput stall, cost-cap approached.

---

## 12. Consent + Data + Security

*(v1 §9, renumbered; deltas folded in.)*

- **What a friend installs:** one signed installer + a tray app. Nothing else. Clear consent screen at install: *"This runs job applications as Jonathan from this machine. It stores his resume/contact info locally and no passwords. It only runs when your machine is idle (or in hours you set), pauses when you're using it, and you can pause or fully remove it anytime."* (§16)
- **What a friend holds:** `profile.json` + `resume.pdf` only, local, encrypted at rest (OS keychain-wrapped key). **No passwords, no LinkedIn, no brain, NO Gmail token, no other people's data.** Friends get **CODES from the relay, never the inbox** (§7.4).
- **What a friend sees:** tray status + their own machine's resource caps/controls (§16). Friend machines run **no-wall applies only** and are **never nagged with captchas** (§7.2). They do **not** see the brain, other machines, or aggregate data.
- **Securing the PG connection:** friends get a **per-machine bearer token to the API broker**, never a Postgres DSN. The broker holds the only DB credential **and the only Gmail token**, exposes a tiny RPC surface (lease/result/challenge/heartbeat/assets/get_answer/request_otp/get_config/poll_commands/report_usage), and rate-limits/audits per token. A leaked token can't dump the queue, touch the brain, or read the inbox — and it's revocable in one row.
- **Revocation / kill-switch:** (a) `fleet_config.paused = true` halts all leasing instantly (global kill). (b) Revoking a machine's broker token stops that machine alone. (c) Tray **Uninstall+wipe** removes assets and the worker. (d) Per-machine token rotation on a schedule. (e) **Remote pause/drain** from the dashboard (§11). Every apply is logged with `worker_id` + `machine_owner` for a consent audit trail.

---

## 13. Cost Governor (new — R14)

Compute (scoring/audit/tailor/enrich) costs **real API money**; apply is $0.

- **Fleet-wide LLM-spend tracking:** every compute call writes an `llm_usage` row (worker, machine_owner, task, model, tokens, `cost_usd`) — reusing the existing `llm_usage` data shape. Each compute node also reports `spend_today_usd` in its heartbeat (§11). The dashboard shows **per-machine + total** spend.
- **Configurable spend cap — single home (E4):** `fleet_config.cost_cap_daily_usd` / `cost_cap_total_usd`. The **compute lease is refused** (the gate in §8) once the rolling daily or total spend exceeds the cap. The governor (§6) does not carry a parallel cost mechanism — it merely references this cap at the compute-lease point. Apply leases are $0 and unaffected.
- **Alerts** when spend approaches the cap (§11).

---

## 14. Fleet Auto-Update / Version Management (new — R12)

Twelve distributed machines must get fixes/features **without version skew.**

- **Broker serves the current pinned worker version** (`fleet_config.pinned_worker_version`); workers **self-update** by polling `get_config` / `poll_commands` and pulling the new package from the broker, then the watchdog relaunches.
- **Staged rollout of updates:** **canary a new version on 1 machine** — `fleet_config.canary_version` + `canary_worker_id` target one worker, and the per-worker `self_update` command carries the target in `remote_commands.target_version` (D8) so the canary pulls exactly that build. **Watch its heartbeat/health/outcomes**, then promote **fleet-wide** by bumping `pinned_worker_version`. A bad update **can't break the whole fleet at once.**
- **Version is reported in the heartbeat** (`sw_version`, §11) so the dashboard shows skew and the canary's health side-by-side with the fleet.

---

## 15. Per-Machine Canary (new — R15)

A new machine, on joining, is **validated BEFORE it's trusted with live applies** — protecting against a misconfigured machine doing bad live applies.

- **SMOKE TEST:** reach the broker, fetch assets, drive a browser (launch Chromium, load a page), report a heartbeat. Confirms the install works end-to-end.
- **DRY-RUN apply:** claim a real job, **fill the form, but do NOT submit** — confirms profile hydration, the answer bank, and the captcha detector all behave on a live ATS.
- Only after both pass is the machine marked `validated` (capability flag) and allowed to claim **live** apply leases. This **fits the staged rollout** (§17) as the gate every new machine passes.

---

## 16. Friend-Machine Stewardship (new — R13)

Being a good guest is what keeps friends running it.

- **Resource caps:** cap CPU / RAM / browser-count (`WORKERS=N`, §5) so a friend's laptop is **never pegged.** Defaults are conservative on laptops (≈2 browsers).
- **Run-on-idle:** the worker runs **only when the machine is idle**, or within **allowed hours** the machine's owner sets.
- **Auto-pause on activity:** detect **foreground user activity** and **auto-pause** immediately; resume when idle again. (Reported as `state='paused'` in the heartbeat.)
- **Friend control from the tray:** the friend can **set limits** (max browsers, allowed hours, CPU/RAM ceiling) and **see/control** the worker (pause, view today's count, uninstall+wipe).

---

## 17. Staged Rollout

Each stage is independently shippable and reversible.

- **S0 — Pick topology (R2).** Default **local SQLite** (Topology A). Stand up the broker + Postgres coordination schema (§3). (Flip to **cloud Postgres-as-brain**, Topology B, later when fleet scale justifies it; that's a config flip + one-time 77k-job migration and the sync bridge disappears.)
- **S1 — Distribute compute only (zero apply risk).** Add `compute_queue`, `push/pull`, the broker, the **cost governor + `llm_usage`** (§13), and the **heartbeat substrate + dashboard skeleton** (§11). Run compute workers on the owner box + 1 cloud node. (Apply-PUSH is not run yet — no `approved_batch` exists until S2; see the §2 rollout note.) *Ship gate:* advisory scores flow back, no brain corruption, spend tracked and capped, heartbeats visible.
- **S2 — One residential apply worker (owner's own second machine).** Add governor tables (now **outcome-aware** §6, incl. the per-IP breaker join §3) + governor-gated claim + `auth_challenge` **detector/router** (§7) + the **answer bank** (§9.2) + the **approval gate** (§9.3) + `applied_set` **dedup** (§9.1) + the **Gmail OTP relay** (§7.4) + the **watchdog** (§11) + tray. New machines pass the **canary** (§15). Run a tiny daily cap (global 10/day). *Ship gate:* applies succeed on residential IP; captchas route to the owner inbox/tray; OTPs auto-solve via relay; unknown questions defer; governor caps + per-IP breaker hold; watchdog recovers a killed worker.
- **S3 — Add 1–2 friend machines, then scale to ~10–12.** Per-machine tokens, consent flow, **friend stewardship** (idle/auto-pause/caps, §16), per-home caps, **WORKERS=N auto-sizing** (§5). Captchas on friend machines **bounce to the owner inbox** (friends never nagged). Raise the global cap gradually while watching per-IP `challenge_rate` and the **adaptive breaker/demote** (§6). *Ship gate:* no IP shows a rising challenge-rate trend; demote/throttle works; consent/audit logging complete; canary passes per machine.
- **S4 — Cloud compute scale-out.** Add proxied cloud compute nodes to `compute_queue`. *Ship gate:* compute throughput up under the cost cap; apply lanes unaffected.
- **S5 — Outcome loop live (R8).** Wire the `inbox_events` scanner (on the trusted spot) → outcomes into the brain and the dashboard; **interview requests surface loudly**; conversion signal feeds scoring; no re-apply after a response. *Ship gate:* a real response ties to its application; an interview pins to the top; an answered posting is excluded from re-apply.
- **S6 — LinkedIn lane (last, owner-only, ONE IP / N machines — R1).** Wire `linkedin_queue` + the `account:linkedin` mutex (§3 LinkedIn lease) as **multi-consumer but single-IP, governor-serialized**; keep the home-side rolling-24h cap. *Ship gate:* LinkedIn applies only ever originate from the one owner IP; never two concurrent sessions; combined rate under the per-account cap.
- **S7 — Auto-update + topology flip (optional).** Turn on **staged self-update** (§14: canary → fleet). If/when scale justifies it, execute the **A→B cloud-brain migration** (§1.5).

---

## 18. Risks + Open Questions

- **Double-apply across machines.** Mitigated by **posting-level (`dedup_key`) dedup** + the **pre-submit `applied_set` guard** (§9.1) + `crash_unconfirmed` pinning, but a confirmed-but-unsynced apply could in theory be re-leased after a long Postgres/home partition (**Topology A only**; Topology B has no sync gap). *Largely closed by R9; residual risk only in long-partition Topology A.*
- **Governor accuracy under clock skew / reclaim.** The cap counter increments on confirmed apply; a job that succeeds but whose `write_result` is lost (worker dies post-submit) under-counts both the cap and the outcome counters. Acceptable (errs toward politeness) but worth monitoring; the adaptive breaker tolerates noise via *rates* not absolutes. (Note: with the v2 outcome counters, walls/blocks DO increment — the under-count risk is only the lost-`write_result` edge, not a "success-only" design.)
- **Adaptive-breaker threshold tuning.** The `challenge_rate` thresholds for throttle/pause/demote are initial guesses. *Open:* learn them per host/IP from observed block-precursors; avoid flapping (hysteresis via `breaker_until`).
- **Friend-machine trust.** A malicious friend could tamper with the local worker. Data minimization caps the blast radius (resume + contact only; **no Gmail token, no passwords**), but *open:* should applies from a machine be signed/attested? The **canary** (§15) catches misconfiguration, not malice.
- **Broker as single point of failure (now also Gmail relay + answer bank + config + version + inbox scanner + asset server).** It's the only path to PG, the only holder of the Gmail token, and the answer/version/asset source; needs HA or fast restart. *Open:* run it on the owner box or a managed host? In Topology B it co-locates with the brain PG. The **watchdog** covers worker recovery, not broker recovery.
- **Gmail relay concurrency.** Disambiguation by sender + 60s window + mark-consumed (§7.4) could still mis-hand a code if two ATSes mail from the same sender within the window. *Open:* tighten with per-request nonces echoed in the OTP email where the ATS supports it.
- **Captcha binding & remote-assist.** Assumed captcha is bound to the browser session/IP that raised it (hence the bounce re-attempts on the *owner's* IP rather than solving the friend's session out-of-band). Remote-assist (§7.2, deferred to a separate proposal) would preserve the friend's session for binding. *Open:* verify per-ATS whether the owner's IP re-attempt succeeds vs. needing remote-assist.
- **Per-host caps & breaker are guesses.** Initial Greenhouse/Lever/Ashby caps, min-gaps, and breaker thresholds are unvalidated. *Open:* instrument per-host outcome rates and auto-tune `daily_cap`/`min_gap_seconds`/thresholds.
- **Answer-bank coverage vs. deferral load.** Early on, many questions are unknown → many owner deferrals (§9.2). Acceptable (the bank learns), but *open:* seed it aggressively from the existing question-bank to minimize cold-start deferrals.
- **Approval-gate throughput.** Owner gating (§9.3) is the deliberate bottleneck that preserves selectivity; *open:* batch-review UX must be fast enough that the gate isn't a chore (else the fleet starves).
- **Cost cap vs. throughput.** A tight LLM spend cap (§13) can starve compute. *Open:* per-task cost budgeting + prioritizing high-value scoring under the cap.
- **Cloud-brain migration (Topology B).** Migrating schema + 77k jobs to PG, accepting a network dependency and data-in-cloud. *Open:* migration runbook + rollback to SQLite; verify `brainDb` PG target parity before the flip; confirm `dedup_key` generation parity in the no-PUSH path (§2/§9.1).
- **Residential ToS / consent durability.** Friends may revoke informally (turn the machine off, or the auto-pause keeps it idle). The defer/timeout + stewardship paths handle it, but *open:* define a clean "machine retired" state that drains its in-flight leases (`remote_commands: drain`).
