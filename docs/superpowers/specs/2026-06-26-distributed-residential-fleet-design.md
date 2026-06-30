# Distributed Residential ApplyPilot Fleet — Design Spec (v3)

**Status:** Proposed (canonical design doc, for owner review). **v3 — folds the four refinements into v2: (1) calibrated WIDE-NET approval policy (auto-band + gray-zone + sampling audit, replacing the simple gate), (2) governed DISCOVERY (splits the compute plane; `board:` governor), (3) a distributed RECURRING search-task scheduler, and (4) the ApplyPilot HELPER worker app + zero-touch onboarding. Preserves all v2 content (18 sections + 15 requirements) and adds two new sections (§8.5, §16.5).**
**Date:** 2026-06-26 (v3)
**Builds on:** shelved Railway datacenter fleet (`src/applypilot/apply/{fleet_schema.sql,pgqueue.py,fleet_sync.py,container_worker.py,launcher.py}`), unified-brain pipeline spec, authoritative SQLite brain (`database.py`), `brainDb.ts` (backend-agnostic data layer), `gmail_outcomes` parser + `inbox_events` scanner, existing question-bank and captcha-probe, the qualification/triage system that produces confidence-bearing verdicts.
**Principle:** Jonathan applies AS HIMSELF, with his own resume, ONLY to jobs his system already scored qualified AND the calibrated approval policy has cleared. Friend machines hold application DATA only — never passwords, never the Gmail token. The fleet is a politeness-bounded, owner-calibrated good-faith applicator — selectivity AUTOMATED, not indiscriminate; wide pool, sustainable drain.

---

## 0. What changed (orientation)

### 0.1 What changed in v2

v1 established the three-plane architecture, the SQLite↔Postgres sync bridge, the lane split, the lease-queue worker model, the global rate governor, the captcha human-in-the-loop, compute distribution, consent/security, and the staged rollout. v2 preserves all of that and folds in fifteen owner requirements. The structural deltas (section numbers below are authoritative):

- **The "brain" is now backend-agnostic** behind the `brainDb` data layer, with TWO supported topologies (local SQLite OR cloud Postgres-as-brain). The sync bridge is now topology-dependent — it *disappears* in the cloud-brain topology. (§1.5 new, §2 expanded)
- **The broker grew up.** It is no longer just an RPC shim to Postgres; it is now also the **Gmail auth-code relay**, the **answer-bank server**, the **config/remote-command channel**, the **asset server**, and the **version server**. (§5, §7, §9, §10, §11, §14 expanded)
- **The governor grew up.** It went from counts+caps to **outcome-aware adaptive** with a per-IP and per-host circuit-breaker, auto-demote, and a **cost dimension** (the cost cap itself lives in `fleet_config`, §13). (§6 expanded)
- **The dashboard is new and central.** Single pane of glass surfacing fleet health, the captcha inbox, outcomes/interviews, and the approval gate. (§11 new, plus hooks throughout)
- **New top-level sections:** Deployment Topologies (§1.5), Apply Quality — Dedup + Answer Bank + Approval Gate (§9), Outcome Tracking + Feedback Loop (§10), Fleet Health & Recovery (§11), Cost Governor (§13), Auto-Update / Version Management (§14), Per-Machine Canary (§15), Friend-Machine Stewardship (§16).
- **The LinkedIn lane relaxed** from "exactly one machine" to "ONE IP, N coordinated machines, governor-serialized." (§1, §4, §6 updated; deliberately reverses a v1 statement — flagged in §6 and §19.)

### 0.2 What changed in v3 (the four refinements)

v3 preserves every v2 section and all fifteen requirements, and folds in four owner refinements. The deltas:

- **RF1 — Approval gate → CALIBRATED WIDE-NET POLICY.** §9.3 is rewritten. The simple "threshold OR batch" gate becomes a calibrated, multi-criteria, outcome-validated, sample-audited POLICY: an **auto-approve band anchored on "high-confidence-qualified"** (strong fit AND the qualification system's confident verdict AND no red-flags) that **auto-stamps `approved_batch` with NO manual click**, defaulting **WIDE**; a **gray-zone review queue** for only the uncertain slice; and a **sampling audit** of what actually went out as the primary human oversight. Crucially, **wide approval ≠ fast blasting** — the §6 governors still pace the actual sends. (§9.3 rewritten; §2 PUSH note; §10 outcome-tuning loop; §11 dashboard panels; §3 `fleet_config.approval_policy`.)
- **RF2 — GOVERNED DISCOVERY (compute-plane correction).** v2 §1 wrongly lumped discovery into "any IP, no captcha" compute. v3 splits the plane into **PURE compute** (genuinely IP-free) and **DISCOVERY** (search/scrape — network + IP/account-SENSITIVE, must be UNAUTHENTICATED, governed by a new `board:<name>` governor scope, residential-spread, LinkedIn-careful). (§1 topology split; §6 governor gates discovery; §8 reframed as pure-compute + a governed discovery lane.)
- **RF3 — DISTRIBUTED SEARCH-TASK SCHEDULER (new §8.5 + schema).** A scheduled, recurring search-task queue that decomposes `searches.yaml` into discrete (query × board × location) tasks; machines CLAIM due tasks, scrape, and push deduped postings to the brain; **searches RECUR** on a per-task cadence; governed by RF2's `board:` governor; resilient (a blocked task re-queues to a different IP); with a dashboard coverage view. (New §8.5; new `search_tasks` table + claim query in §3.)
- **RF4 — FLEET WORKER APP & ONBOARDING (new §16.5).** A thin cross-platform **"ApplyPilot Helper"** app (wraps `container_worker.py` + bundled Chromium; talks only to the broker), **zero-touch enrollment** (personalized installer with the per-machine token baked in; the link IS the enrollment), **two config layers** (owner-central + friend-local), **maintenance-free + non-technical-safe** (auto-update, watchdog, NO technical error ever shown to the friend — all errors route to the owner), **invisible by default** (stewardship §16), and **manual-walkthrough-first / notarization-when-ready** install signing. (New §16.5; ties to §5 worker, §12 consent, §15 canary, §16 stewardship, R12 auto-update.)

The section numbering is otherwise unchanged from v2; v3 inserts §8.5 and §16.5, rewrites §9.3, and renumbers the v2 Risks section from §18 to §19 (with §18 left as a reserved pointer) so the new §16.5 sits adjacent to stewardship.

---

## 1. Architecture Overview + Topology

The system has three planes that share **one coordination layer** and write back to **one authoritative brain** (which backend hosts that brain is a deployment choice — see §1.5):

- **Brain plane:** ~77k jobs, Python-owned schema, the only durable store, reached through the backend-agnostic `brainDb` data layer. In the default topology it is SQLite at `%LOCALAPPDATA%/ApplyPilot/applypilot.db` on the owner's home box; in the cloud topology it is Postgres (see §1.5). Pushes work, pulls results — or *is* the queue directly, depending on topology.
- **Compute plane — split into two lanes (corrected in v3, RF2):**
  - **PURE compute (cloud + residential, any IP):** score / audit / triage / tailor / enrich-text. **Genuinely IP-free** — no captcha, no IP sensitivity → free horizontal parallelism. Reads job rows from `compute_queue`, writes advisory results back to the brain. Costs real API money — tracked by the cost governor (§13). Datacenter/cloud IPs are perfectly fine here.
  - **DISCOVERY (search/scrape — its own GOVERNED lane):** search and scrape job boards (LinkedIn / Indeed / Greenhouse boards / company sites) to find postings. This is **network + IP/account-SENSITIVE**, NOT pure compute: scraping search endpoints is per-IP rate-limited, and LinkedIn scraping is account/ban-sensitive. Discovery therefore runs **UNAUTHENTICATED** (never logged into the apply account), is **spread across RESIDENTIAL IPs** (which *helps* — it dilutes per-IP scrape load), and is **governed** by a new `board:<name>` scope in the §6 governor parallel to `host:<domain>` for applies. Driven by the distributed search-task scheduler (§8.5).
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
                            PUSH ▲       │ PULL (results + outcomes + postings → brain)
                        (work)   │       ▼          (bridge VANISHES in cloud-brain topo §1.5)
                  ┌──────────────┴────────────────────────────────────────┐
                  │   POSTGRES (coordination; OR the brain itself §1.5)    │
                  │  compute_queue | apply_queue | linkedin_queue          │
                  │  search_tasks (RECURRING scheduler §8.5)               │
                  │  rate_governor (outcome-aware; host: AND board: scope) │
                  │  results sink | auth_challenge / CAPTCHA INBOX         │
                  │  applied_set (dedup) | answer_bank | inbox_events      │
                  │  worker_heartbeat | poison_jobs | fleet_config         │
                  │  llm_usage (cost) | remote_commands | otp_request      │
                  └─┬────────┬─────────────┬──────────────┬────────┬───────┘
                    │        │             │              │        │
          lease     │ lease  │   lease     │    lease     │ serves │ reads (single pane)
        (no-login)  │(PURE   │ (DISCOVERY  │  (LinkedIn)  │(RPC/   │
                    │ compute│  scrape —   │              │ relay) │
                    │ any IP)│  GOVERNED   │              │        │
                    │        │  board:)    │              │        │
   ┌────────────────▼┐ ┌─────▼────────┐ ┌──▼──────────┐ ┌─▼─────┐ ┌▼────────────────────┐
   │ RESIDENTIAL APPLY│ │ PURE COMPUTE │ │ DISCOVERY    │ │LinkedIn│ │  THIN API BROKER     │
   │ WORKERS #1..#12  │ │ (proxied/any │ │ SCRAPERS     │ │workers │ │  • lease/result RPC  │
   │ (friends + owner)│ │  IP — IP-free│ │ (residential │ │behind  │ │  • Gmail code RELAY  │
   │ Helper app §16.5 │ │  score/audit/│ │  IPs, UNAUTH,│ │the ONE │ │  • ANSWER-BANK serve │
   │ WORKERS=N each   │ │  tailor)     │ │  governed by │ │owner IP│ │  • SEARCH-CONFIG serve│
   │ fill-as-Jon      │ │ heartbeat+   │ │  board:scope)│ │(gov-   │ │  • asset server      │
   │ heartbeat→broker │ │  cost        │ │ push postings│ │serial- │ │  • config + remote   │
   │ captcha→OWNER    │ └─────┬────────┘ │ → brain      │ │ized)   │ │    commands          │
   │  inbox           │ ┌─────▼────────┐ │ (dedup_key)  │ └────────┘ │  • version server    │
   │ watchdog + canary│ │ RESIDENTIAL  │ └──────┬───────┘            │  • holds the ONLY DB │
   └────────┬─────────┘ │ CMP (idle CPU)│        │ heartbeat/        │    cred + Gmail token│
            │           │ heartbeat+cost│        │ scrape-outcomes   │  • inbox SCANNER →   │
            │           └─────┬─────────┘        │                   │    inbox_events      │
            │ heartbeat /     │ heartbeat/cost   │                   └──────────┬───────────┘
            │ outcomes/health │                  │                              │ reads
            ▼                 ▼                  ▼                              │
   ┌──────────────────────────────────────────────────────────────────────────▼──────────┐
   │  CENTRALIZED DASHBOARD (owner box or broker; reads PG) — single pane of glass         │
   │  fleet health • per-IP captcha/block + throttle/demote • queue vs caps •              │
   │  CAPTCHA INBOX • INTERVIEW REQUESTS (loud) • APPROVAL POLICY (auto-band / gray-zone /  │
   │  sampling audit §9.3) • SEARCH COVERAGE (which searches live/last-refresh §8.5) •      │
   │  quarantine • cost • ENROLL "Add a machine" §16.5                                      │
   │  ACTIONS: restart worker • pause machine • clear captcha • tune auto-band • review     │
   │           gray-zone • spot-check sampled-applied • prune dead search • add machine     │
   └───────────────────────────────────────────────────────────────────────────────────────┘
```

**Key boundaries.**
- **PURE compute may run anywhere** (datacenter IPs are fine — no anti-bot wall, no IP/account sensitivity).
- **DISCOVERY is NOT pure compute (RF2).** It is network + IP/account-sensitive: scraping search endpoints IS per-IP rate-limited, and LinkedIn scraping is account/ban-sensitive (owner rule: "never scrape on the apply account"). Discovery therefore runs UNAUTHENTICATED, is spread across residential IPs (which *helps* dilute per-IP scrape load), and is **governed** by the `board:<name>` scope (§6). LinkedIn discovery especially: unauthenticated, spread, paced, never on the apply account, and (like the LinkedIn apply lane) single-owner-IP.
- No-login ATS applies MUST run on residential IPs (datacenter failed 0/18 on captcha).
- **LinkedIn: ONE IP, N coordinated machines (R1).** v1 said "exactly one machine." We relax this: the takeover-ban risk is driven by **one account being driven from many *public IPs***, not many *processes*. Two (or more) machines in the owner's house share **one public IP**, so the many-IPs ban risk does **not** apply to them. The governor **SERIALIZES** these machines — never two concurrent automated sessions on the account at once — and the **COMBINED** rate stays under the ~20/day per-account cap. `linkedin_queue` therefore allows **multiple consumers, but only from the single owner IP**, governor-serialized. This is **redundancy and flexibility** (the owner's laptop can pick up if the tower is busy), **not extra throughput** — the per-account cap and serialization are unchanged. A machine on a *different* public IP is still forbidden from the LinkedIn lane. **(The same single-owner-IP rule applies to LinkedIn *discovery* scraping — §8.5 — but unauthenticated.)**

---

## 1.5. Deployment Topologies (R2)

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
- The governor, dedup set, answer bank, outcomes, heartbeats, **search-task scheduler (§8.5)**, and dashboard read from the same logical tables; in Topology A they live in the coordination PG, in Topology B they live in the brain PG (same instance). The CREATE TABLE sketches in §3 are written so they're valid in **both** (in Topology A they are coordination tables; in Topology B they are brain tables — the SQL is identical).
- Switching topologies is a config flip + (for A→B) a one-time migration; no worker/broker/dashboard code changes.

---

## 2. Source of Truth + Sync Model

**(Topology A — local SQLite.)** SQLite brain is authoritative and permanent. Postgres is a transient coordination bus: a work queue, a global rate governor, a results sink, and (new in v2) the home for outcomes/dedup/answer-bank/heartbeat/health tables (and, new in v3, the `search_tasks` scheduler — §8.5). Nothing lives in coordination-PG beyond a job's in-flight lifecycle and the rolling operational state; if Postgres were wiped, the brain reconstructs every queue from scratch (operational state like heartbeats/outcomes re-accrues; the `search_tasks` set re-expands from the centralized `searches.yaml` — §8.5 — and, because re-expansion rebuilds tasks fresh, the per-task cadence/freshness state — `last_run_at`/`result_count`/`new_count`/`consecutive_blocks`/`next_due_at` — is NOT preserved across a wipe: every task simply resets to immediately-due and re-accrues).

**Sync directions (exactly two, both idempotent, keyed on `url`):**

- **PUSH (brain → Postgres):** `fleet_sync.push_offsite_jobs()` selects rows the qualification + approval pipeline has cleared (§9.3): they are **not applied/in-flight**, `liveness_status != 'dead'`, `application_url LIKE 'http%'`, LinkedIn excluded, AND they carry an **`approved_batch` stamp** — either auto-stamped by the wide-net auto-rule (strong fit `min_fit` AND the triage system's confident-qualified verdict `min_confidence` AND no red-flags) or released from the gray-zone queue (see §9.3). (`min_fit` reads `COALESCE(audit_score, fit_score)`; `min_confidence` reads the qualification/triage system's confidence-bearing verdict field — see §9.3-A for the source binding. Score is now just one of three auto-rule criteria, not the headline gate.) It computes `dedup_key = (company, normalized_role)` (§9.1) and UPSERTs ~7 routing columns (incl. `dedup_key`, `approved_batch`, `approval_source`) into `apply_queue`. A parallel `push_compute_jobs()` fills `compute_queue` with rows needing scoring/audit/tailor. A parallel `push_linkedin_jobs()` fills `linkedin_queue` (LinkedIn targets only). **Newly discovered postings flow the OTHER way** — discovery scrapers push found postings into the coordination layer (Topology A) / brain (Topology B), deduped by `dedup_key` (§8.5), where they enter the normal score→qualify→approve pipeline. UPSERT only touches `queued` rows, so re-push never disturbs in-flight work.
  - **Rollout note (resolves the staging conflict).** The `approved_batch IS NOT NULL` predicate is the global rule **once the approval policy ships in S2**. Before S2 (compute/discovery-only, S1/S1.5) the apply-PUSH is simply **not run** — no apply rows exist yet — so the predicate is not a contradiction, it just isn't exercised until `approved_batch` exists. Under the wide-net policy (§9.3) the auto-rule stamps `approved_batch` automatically, so PUSH draws from an auto-stamped pool plus any released gray-zone rows.
- **PULL (Postgres → brain):** `fleet_sync.pull_results()` reads terminal rows (`synced_to_home_at IS NULL`), writes them into the brain (`_PULL_APPLIED` / `_PULL_TERMINAL`, never demoting a confirmed apply), commits, then stamps `mark_synced`. **v2 also pulls outcomes** (`inbox_events` → application response, §10) on the same loop. **v3 also pulls discovered postings + scrape outcomes** (new postings deduped into the brain; `search_tasks.result_count`/`new_count` for the coverage view, §8.5) on the same loop. A crash between write and stamp just re-pulls — replay is a no-op behind the `apply_status != 'applied'` guard.

**Cadence.** PUSH on a 5-min loop (or on-demand after a scoring batch / after the policy auto-stamps a batch / after the owner releases a gray-zone batch). PULL on a 60-sec loop while any worker is active. Compute results, discovered postings, and outcomes PULL on the same 60-sec loop.

**Reconciling the single-owner DB service.** The lock-contention audit's recommendation stands, but at the *right layer*: SQLite writes are serialized **at the home process** (WAL mode + `busy_timeout=60s` + thread-local connections), which is the only writer. Postgres needs no serialization — `FOR UPDATE SKIP LOCKED` gives N workers distinct rows lock-free. So: **Postgres = distributed coordination; SQLite = authoritative local store; `fleet_sync` = the bridge.** When research+apply eventually run 24/7, wrap the brain in a localhost single-owner service (unified-brain Option C); until then the on-demand bridge is sufficient. No friend machine ever touches SQLite.

**(Topology B — cloud Postgres-as-brain.)** This entire section reduces to: *there is no sync.* The eligibility predicate above becomes the `WHERE` clause of the lease query directly against brain tables; `synced_to_home_at`/`mark_synced` are unused; outcomes and discovered postings write straight into the brain PG (by the broker/scanner/scraper, not by an apply worker — §1.5/§8.5). `dedup_key` is computed at the moment a job becomes apply-eligible (a generated/trigger-maintained column on the brain's jobs view) rather than in PUSH. See §1.5.

---

## 3. Postgres Schema

Reuse `apply_queue` (lease model, status enum, reclaim indexes) verbatim and add residential columns + the new tables. (In Topology A these are coordination tables; in Topology B they are brain tables — identical SQL.)

```sql
-- Reused, with residential additions:
ALTER TABLE apply_queue ADD COLUMN worker_home_ip   TEXT;     -- sending residential IP
ALTER TABLE apply_queue ADD COLUMN target_host      TEXT;     -- effective apply host
ALTER TABLE apply_queue ADD COLUMN lane             TEXT DEFAULT 'ats';  -- 'ats'
ALTER TABLE apply_queue ADD COLUMN dedup_key        TEXT;     -- (company, normalized_role) §9.1
ALTER TABLE apply_queue ADD COLUMN approved_batch   TEXT;     -- approval token §9.3 (auto OR gray-zone-released)
ALTER TABLE apply_queue ADD COLUMN approval_source  TEXT;     -- 'auto_band'|'gray_zone'|'manual' §9.3
ALTER TABLE apply_queue ADD COLUMN audit_sampled    BOOLEAN DEFAULT false; -- flagged for post-hoc spot-check §9.3
-- est_cost_usd stays but is 0 for residential (no cloud charge).

-- Compute queue (PURE compute only — score/audit/tailor/enrich; genuinely IP-free; any IP):
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
-- NOTE (RF2): DISCOVERY is NOT in compute_queue. Discovery scraping is IP/account-sensitive and
-- lives in its own GOVERNED lane, the recurring search_tasks scheduler below (§8.5).

-- DISTRIBUTED SEARCH-TASK SCHEDULER (new — RF3/§8.5). Decomposes searches.yaml into discrete,
-- RECURRING (query × board × location) tasks. Governed by the board:<name> governor (RF2/§6).
CREATE TABLE search_tasks (
  task_id        TEXT PRIMARY KEY,        -- hash(query, board, location, params) — STABLE across
                                          --   cadence cycles (the row is updated in place, not re-minted)
  query          TEXT NOT NULL,           -- search query string
  board          TEXT NOT NULL,           -- 'linkedin'|'indeed'|'greenhouse'|'lever'|... → board:<name> governor
  location       TEXT,                    -- location facet of the search
  params         JSONB,                   -- remaining search facets (date filter, radius, etc.)
  status         TEXT NOT NULL DEFAULT 'queued',  -- 'queued'|'leased'|'done'|'blocked'
  lease_owner    TEXT,
  lease_expires_at TIMESTAMPTZ,
  next_due_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- becomes claimable again when due (RECURS)
  cadence_seconds INTEGER NOT NULL DEFAULT 14400,     -- refresh cadence (e.g. every 4h)
  attempts       INTEGER DEFAULT 0,       -- retries since last successful run
  last_run_at    TIMESTAMPTZ,
  last_worker    TEXT,
  last_home_ip   TEXT,                    -- which residential IP last ran it (IP-spread visibility)
  result_count   INTEGER DEFAULT 0,       -- postings found on the last run (coverage view §8.5/§11)
  new_count      INTEGER DEFAULT 0,       -- NEW (non-duplicate) postings on the last run
  consecutive_blocks INTEGER DEFAULT 0,   -- scrape-block streak → feeds board: breaker
  enabled        BOOLEAN DEFAULT true,    -- owner can prune a dead search (§8.5/§11)
  updated_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_search_due ON search_tasks (board, next_due_at)
  WHERE status='queued' AND enabled;

-- LinkedIn queue (ONE owner IP; N coordinated machines; governor-serialized — R1):
CREATE TABLE linkedin_queue (LIKE apply_queue INCLUDING ALL);  -- same shape, separate lane
-- Allowed consumers are owner-owned worker_ids whose registered public IP == the owner IP.
-- Serialization is enforced by the 'account:linkedin' rate_governor row (see §6 + claim below).

-- GLOBAL rate governor — OUTCOME-AWARE + ADAPTIVE (R6). NOW ALSO GATES DISCOVERY SCRAPING (RF2)
-- via a board:<name> scope parallel to host:<domain>. Cost CAP lives in fleet_config (§13);
-- this table carries only apply/scrape-rate + outcome state, not a parallel cost mechanism.
CREATE TABLE rate_governor (
  scope_key      TEXT PRIMARY KEY,       -- 'global' | 'host:greenhouse.io' | 'home_ip:1.2.3.4'
                                         -- | 'account:linkedin'  (the LinkedIn apply mutex, §6/R1)
                                         -- | 'board:linkedin' | 'board:indeed'  (DISCOVERY scrape, RF2/§8.5)
  window_start   TIMESTAMPTZ NOT NULL,   -- rolling 24h anchor
  count_24h      INTEGER NOT NULL DEFAULT 0,   -- CAP counter: applies → on CONFIRMED APPLY;
                                               -- board: scopes → on each scrape run
  daily_cap      INTEGER NOT NULL,       -- per-host/global/per-home/per-account/per-board cap
  last_applied_at TIMESTAMPTZ,           -- per-host/per-account min-gap; ALSO per-board scrape min-gap
  min_gap_seconds INTEGER NOT NULL DEFAULT 90,
  -- outcome-aware adaptive fields (R6) — these increment on their OWN terminal class,
  -- NOT only on success (this is the deliberate change from v1's "success-only" counters):
  success_24h    INTEGER NOT NULL DEFAULT 0,   -- ++ on confirmed apply (or clean scrape, board: scope)
  captcha_24h    INTEGER NOT NULL DEFAULT 0,   -- ++ on a visible-captcha / OTP wall
  block_24h      INTEGER NOT NULL DEFAULT 0,   -- ++ on a hard block / CF / SCRAPE-BLOCK (board: scope, RF2)
  challenge_rate NUMERIC GENERATED ALWAYS AS   -- leading indicator of flagging (apply OR scrape)
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

-- Cross-board DEDUP / distributed double-apply guard (R9). ALSO the discovery dedup target (§8.5):
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
  roles        TEXT,                     -- comma-set of roles this machine carries (any combination):
                                         --   'apply','compute','discovery' (§5 — a machine may carry any/all)
  state        TEXT,                     -- 'idle'|'applying'|'searching'|'challenge_pending'|'paused'
  current_job  TEXT,                     -- url (apply/compute) or task_id (discovery) in flight
  job_started_at TIMESTAMPTZ,            -- for over-max-duration detection
  success_today INTEGER DEFAULT 0,
  captcha_today INTEGER DEFAULT 0,
  block_today   INTEGER DEFAULT 0,
  spend_today_usd NUMERIC DEFAULT 0,     -- compute nodes report rolling LLM spend (§13)
  cpu_pct      NUMERIC,
  ram_pct      NUMERIC,
  browser_count INTEGER,                 -- WORKERS=N concurrency (R5)
  sw_version   TEXT,                     -- reported version (R12 / §16.5)
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

-- Fleet config / remote commands / version pin (R7 remote-restart, R12 auto-update).
-- NOTE (RF1): the approval POLICY now lives here as structured JSON, replacing the scalar threshold.
CREATE TABLE fleet_config (
  key          TEXT PRIMARY KEY,         -- 'paused'|'pinned_worker_version'|'canary_version'
                                         -- |'canary_worker_id'|'cost_cap_daily_usd'
                                         -- |'cost_cap_total_usd'
                                         -- |'approval_policy'        (RF1 — JSON, see below; replaces approval_threshold)
                                         -- |'approval_sampling_rate' (RF1 — random spot-check %)
                                         -- |'search_config'          (RF3 — searches.yaml, served to scrapers)
                                         -- |...
  value        TEXT
);
-- approval_policy JSON shape (RF1 / §9.3):
--   { "default_width": "wide",
--     "auto_rule":   { "min_fit": 7, "min_confidence": "qualified_confident",
--                      "exclude_flags": ["stretch","pivot_penalty","location_blocker",
--                                        "work_auth_blocker","comp_dealbreaker"] },
--     "gray_zone":   { "fit_lo": 5, "fit_hi": 7, "confidence_lo": "borderline" } }
--   min_fit reads COALESCE(audit_score, fit_score); min_confidence reads the qualification/triage
--   system's confidence-bearing verdict field (§9.3-A). approval_sampling_rate: e.g. 0.05 → 5% of
--   ACTUALLY-APPLIED rows flagged audit_sampled=true.

CREATE TABLE remote_commands (
  id           BIGSERIAL PRIMARY KEY,
  worker_id    TEXT,                     -- target ('*' = fleet-wide)
  command      TEXT,                     -- 'restart'|'pause'|'resume'|'self_update'|'drain'
  target_version TEXT,                   -- for 'self_update': which version to pull (R12 canary)
  issued_at    TIMESTAMPTZ DEFAULT now(),
  acked_at     TIMESTAMPTZ
);
```

**Atomic claim with expiry (governor-aware + outcome-aware + per-IP breaker + dedup + approval).** Extend the existing `lease_one` so the lease is *refused* when (a) any global counter is over cap, (b) the host's **OR the worker's home-IP's** breaker is throttled/paused/demoted or over cap, (c) the posting is already in the `applied_set`, or (d) the job isn't in an approved batch (auto-band-stamped OR gray-zone-released, §9.3). A single statement leases the highest-scored eligible job whose host AND home-IP are (i) past the (possibly widened) min-gap, (ii) under both per-scope and the global daily cap, (iii) `breaker_state='ok'`, (iv) not already applied, (v) approved:

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
    AND q.approved_batch IS NOT NULL                            -- R11 approval (auto OR gray-zone) §9.3
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

> **Note (RF1).** The approval predicate is unchanged in the claim — it remains `approved_batch IS NOT NULL`. What changed is *who stamps it*: under the wide-net policy the **auto-rule stamps `approved_batch` automatically** (no manual click), and gray-zone rows get stamped only when the owner releases them. The governor pacing above is identical regardless of how wide the approved pool is — a wide pool simply drains over more days (RF1: wide approval ≠ fast blasting).

**LinkedIn lease (D1 FIX — the `account:linkedin` mutex made concrete).** LinkedIn has its own claim. Eligible workers are owner-IP machines; serialization is the `account:linkedin` governor row whose `min_gap_seconds` acts as a **mutex** (a held lease stamps `last_applied_at`, so no second owner machine can lease until the gap elapses → **never two concurrent automated sessions**, combined rate under the ~20/day cap). The approval predicate (`q.approved_batch IS NOT NULL`) and the cap/breaker/gap gates are all explicitly parenthesized so they ALL bind (RF1-a fix — approval is on the *job* row `q`, not on the `account:linkedin` governor row; and no `OR` may bypass the cap/breaker):

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
    AND q.approved_batch IS NOT NULL                            -- approval-gated like ATS §9.3 (on q, the job row)
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

**Search-task claim (new — RF3/§8.5; governed by the `board:` scope — RF2).** A scraper leases the highest-priority *due* search task whose board governor is healthy and past its scrape min-gap. Recurrence is the key twist: a task is claimable only when `next_due_at <= now()`; on completion the worker reschedules it `cadence_seconds` into the future. A scrape-block trips the `board:` breaker (RF2/§6) and re-queues the task to a different machine/IP.

```sql
WITH bgov AS (
  SELECT count_24h, daily_cap, last_applied_at, min_gap_seconds, breaker_state
  FROM rate_governor WHERE scope_key = 'board:' || %(board)s
),
next_task AS (
  SELECT t.task_id
  FROM search_tasks t CROSS JOIN bgov b
  WHERE t.status = 'queued'
    AND t.enabled
    AND t.board = %(board)s
    AND t.next_due_at <= now()                                  -- RECURS: only claimable when due
    AND b.breaker_state = 'ok'                                  -- RF2 per-board scrape breaker
    AND b.count_24h < b.daily_cap                               -- per-board daily scrape cap
    AND (b.last_applied_at IS NULL                              -- per-board scrape min-gap (paced)
         OR b.last_applied_at < now()
            - make_interval(secs => b.min_gap_seconds * (0.7 + random()*0.7)))
  ORDER BY t.next_due_at ASC                                    -- oldest-due first → freshness fairness
  LIMIT 1
  FOR UPDATE OF t SKIP LOCKED                                   -- claimed ONCE per cadence
)
UPDATE search_tasks t
SET status='leased', lease_owner=%(worker)s,
    lease_expires_at = now() + make_interval(secs => 600),      -- short lease; scrapes are quick
    attempts = t.attempts + 1, last_worker = %(worker)s,
    last_home_ip = %(home_ip)s, updated_at = now()
FROM next_task WHERE t.task_id = next_task.task_id
RETURNING t.task_id, t.query, t.board, t.location, t.params;
-- The broker also stamps board:<name>.last_applied_at on lease (paces the next scrape).
-- On a clean run: status='queued', next_due_at = now() + cadence_seconds, result_count/new_count set,
--   board:<name>.success_24h++, consecutive_blocks=0.
-- On a scrape-block: board:<name>.block_24h++, consecutive_blocks++, lease released (status='queued',
--   next_due_at unchanged) → re-queues to a DIFFERENT machine/IP; a streak trips the board: breaker (§6).
-- LinkedIn-search tasks (board='linkedin'): UNAUTHENTICATED, single-owner-IP, careful gap (RF2/§8.5).
```

**Counter semantics (C3 — reconciled, authoritative).** Inside the same transaction as `write_result`:
- `count_24h` (the **cap** counter) increments **on confirmed apply only** for apply scopes, and **on each scrape run** for `board:` scopes — it is what the daily caps gate against.
- The **outcome** counters increment on their respective terminal classifications: `success_24h++` on confirmed apply (or clean scrape), `captcha_24h++` on a captcha/OTP wall, `block_24h++` on a hard block / CF (or **scrape-block** for `board:` scopes, RF2). These drive `challenge_rate` and the adaptive breaker (§6). This is the deliberate change from v1's "success-only" model: walls and blocks now count (that is the whole point of R6).

On confirmed apply, the same transaction UPSERTs `applied_set(dedup_key)` (§9.1) and, if the row was selected by the sampling audit, leaves `audit_sampled=true` for the post-hoc spot-check (§9.3). **Reclaim** (unchanged): a sweep flips `leased` rows whose `lease_expires_at < now()` back to `queued` — a dead/offline worker's job re-queues automatically (apply/compute TTL 1200s > 900s job timeout + grace; search tasks use a 600s lease, see §11.2 for the discovery-specific over-max threshold). A `challenge_pending` lease is *frozen*, not reclaimable (§7).

---

## 4. Lane Split Enforcement

Three mechanisms, defense-in-depth:

1. **Queue partitioning.** LinkedIn *apply* targets go to `linkedin_queue` (never `apply_queue`). `push_offsite_jobs` already filters `application_url NOT LIKE '%linkedin.com%'`; a symmetric `push_linkedin_jobs` routes the inverse. No-login ATS lives only in `apply_queue` (`lane='ats'`). **PURE compute** lives only in `compute_queue` (no IP gate). **DISCOVERY** lives only in `search_tasks` (governed by `board:`, §8.5) — it is never mixed into `compute_queue` (RF2: discovery is IP/account-sensitive, compute is not).
2. **Worker capability flags.** Each worker registers a capability set when it connects: residential apply workers advertise `{can_ats: true, can_linkedin: false}`; discovery scrapers advertise `{can_discovery: true}`, **and for `linkedin`-board search tasks the flag is granted only if the worker's registered public IP equals the owner IP and the worker runs UNAUTHENTICATED** (so the IP constraint is encoded at the capability layer too, not only at the server guard — symmetry with the LinkedIn apply lane); the owner's machines advertise `{can_linkedin: true}` **only if their registered public IP equals the owner IP**. The lease RPC filters by lane (and, for `linkedin`-board discovery, by IP) the worker is allowed to claim. `linkedin_queue` is now **multi-consumer but single-IP (R1):** any owner-owned worker whose public IP == the owner IP may lease from it, but the **governor serializes** them via the `account:linkedin` mutex (§3 LinkedIn lease, §6) so at most one LinkedIn session is automated at a time.
3. **Server-side guard.** A Postgres trigger rejects any `apply_queue` insert whose `target_host` matches `linkedin.com`, and the broker rejects any `linkedin_queue` lease whose worker's registered public IP `!=` the owner IP. The broker likewise rejects any `linkedin`-board `search_tasks` lease from a non-owner IP, and rejects any discovery lease that presents apply-account credentials (RF2: discovery is UNAUTHENTICATED). Even a misconfigured worker (or a friend machine on a different IP) cannot cross into the LinkedIn apply lane or the LinkedIn-discovery lane.

---

## 5. Worker Model (Residential) + Easy Playwright Scaling (R5)

Each residential machine runs a **thin Python worker** (reuse `container_worker.py`'s loop, swap env), wrapped for non-technical friends in the **ApplyPilot Helper app** (§16.5). A machine runs **`WORKERS=N` concurrent worker slots**, each its own Chromium instance:

**Per-machine concurrency (R5).** `WORKERS=N` is **auto-sized to the machine's RAM/CPU** by default (friend laptop ≈ 2, owner tower ≈ 8), overridable in the tray / config (and capped by stewardship limits, §16). Each worker slot runs an independent Chromium via Playwright with its own persistent residential Chrome profile. **Scaling is trivial because of the lease queue:** a new worker slot — or a whole new machine — just starts **CLAIMING**; there is **no central reconfig or rebalance**. Horizontal (more machines) and vertical (more browsers per machine) are the *same operation*: "add workers." It **stays safe automatically** — the global governor's per-host min-gap (§6) serializes hits to any one host *regardless of how many browsers exist*, so extra browsers simply let slow browser/captcha-wait time on one host overlap with applies to *different* hosts. More browsers never means more hits/host/sec. (The same applies to discovery scrapers under the `board:` min-gap, RF2/§8.5.)

**Loop (apply role):** `lease_one()` (ATS lane, governor-gated, dedup-gated, approval-gated) → hydrate Jonathan's data → drive a real browser (Playwright + a persistent residential Chrome profile) to fill the no-login form as Jonathan → for each screening question, **ask the broker's answer bank (§9.2)**; an unknown question DEFERS to the owner rather than guessing → run the **captcha detector/classifier (§7)**; on an email-OTP wall, request the code from the **Gmail relay (§7.4)** and continue; on any human-needed wall, **park and move on** (never block) → else submit, verify confirmation, `write_result()` (increments cap counter + outcome counters, UPSERTs `applied_set`, honors the §9.3 sampling flag). Every ~20s it emits a **heartbeat (§11)**.

**Loop (discovery role — RF3/§8.5):** `lease_search_task()` (governed by `board:`) → run the unauthenticated search/scrape → extract postings → push them to the brain deduped by `dedup_key` → reschedule the task `cadence_seconds` ahead → heartbeat. A scrape-block parks the task for re-queue to a different IP (RF2). A machine may carry the apply role, the discovery role, the compute role, or any combination (heartbeat `roles`).

**What it holds (data minimization):** `profile.json` (name, email, phone, work history, links) + `resume.pdf`, served by **the broker's `fetch_assets`** RPC (or pushed once at install via the Helper, §16.5). There is no `fleet_assets` Postgres table — assets are **broker-served blobs** (C1 fix), consistent with the "friends never get a Postgres DSN" rule. **No passwords, no LinkedIn cookies, no SQLite brain, no broad DB creds, and crucially NO Gmail OAuth token** (§7.4). Assets live in the worker's local `%LOCALAPPDATA%/ApplyPilot/` (Win) or `~/Library/Application Support/ApplyPilot/` (Mac) and are deletable on revocation.

**PG auth (no broad creds — see §9/§12):** the worker authenticates to a **thin API broker**, not to Postgres directly. It holds a per-machine bearer token; the broker exposes `lease`, `write_result`, `raise_challenge`, `heartbeat`, `fetch_assets`, **`get_answer` (answer bank)**, **`request_otp` (Gmail relay)**, **`get_config` / `poll_commands` (remote control + version)**, **`report_usage` (cost)**, and (new in v3) **`lease_search_task` / `push_postings` / `get_search_config` (discovery scheduler — §8.5)**. Friends never get a Postgres DSN.

**Offline / resume behavior (stateless self-resume — R7).** No heartbeat for > lease TTL → the reclaim sweep re-queues the job; another machine picks it up. On restart, the worker simply **resumes leasing — there is no recovery state to rebuild**; the missing-heartbeat lease auto-reclaims. In-flight work that crashed mid-submit lands as `crash_unconfirmed` (pinned, never blind-retried — protects against double-apply, and gated again by the §9.1 dedup check). A crashed search task simply re-queues at its existing `next_due_at` (coverage not lost — RF3).

**Install footprint.** One installer per OS, delivered as the **ApplyPilot Helper app (§16.5)**: Python 3.12 runtime (embedded) + Playwright Chromium + the worker package + a tray app + a **per-machine watchdog (§11)**. Win: `.exe` / scheduled task; Mac: **a `.app` (NOT a `.pkg` — see §16.5)** / `launchd` agent. **Signing posture is manual-walkthrough-FIRST, notarization-WHEN-READY (§16.5):** the first installs ship unsigned with a version-aware Gatekeeper/SmartScreen walkthrough; Apple notarization + Windows code-signing are set up in parallel and flipped in before scaling to the full group. Tray shows status (idle / applying / searching / **needs you** for an owner-machine challenge), `WORKERS=N` and resource caps (§16), and a one-click **Pause** and **Uninstall+wipe**.

---

## 6. Global Rate Governor — Outcome-Aware & Adaptive (R6, R1, RF2)

The governor turns N independent machines into one polite client — **for both applies AND discovery scraping (RF2)**. It now does **counts + caps + an outcome-aware circuit-breaker**, all enforced **inside the atomic claim** (§3) so they're true fleet-wide, not per-process. (The **cost** cap is a separate concern with a single home in `fleet_config`; the governor only *references* it via the compute-lease gate, §8/§13 — there is no parallel cost mechanism here, avoiding drift — E4 fix.)

**Counts & caps (from v1, plus the v3 `board:` scope):**
- **Per-host min-gap + jitter:** `rate_governor.last_applied_at` per `host:<domain>`, with `min_gap_seconds * (0.7 + random()*0.7)`. A worker cannot lease a job for a host another worker just hit — the gap is global because the timestamp lives in shared Postgres, not in any machine's memory. **This is also what makes WORKERS=N safe (R5).**
- **Per-board scrape min-gap + jitter (new — RF2):** an exactly-parallel `board:<domain>` scope paces *discovery* scrapes: a scraper cannot lease a search task for a board another scraper just hit. A scrape-block increments `block_24h` on the `board:` row → trips the same adaptive breaker → throttles/pauses scraping on that board for the offending IP. **A scrape-block is a leading indicator** (just like a captcha for applies) → the IP cools off for discovery before a hard scrape-ban.
- **Per-host daily cap:** `count_24h < daily_cap` per host (e.g. Greenhouse 60/day, Lever 50/day fleet-wide). Tunable per host as anti-bot tolerance is learned. **Per-board daily scrape cap** is the discovery analogue.
- **Global daily cap:** `scope:'global'` row caps total fleet applies/day (e.g. 200). Replaces the datacenter `$200 spend cap` (residential apply cost is $0; LLM cost is governed separately in §13).
- **Per-home cap:** `home_ip:<ip>` row limits any single residential IP, so one friend's line isn't overrepresented to a host. **(Now enforced in the claim — see D2 fix in §3.)** This per-home limiting covers *both* applies and scrapes from that IP.

**LinkedIn serialization (R1).** LinkedIn is single-account. The governor enforces an `account:linkedin` scope row whose **min-gap acts as a mutex**: a LinkedIn lease takes the row lock and stamps `last_applied_at`, so a second owner-IP machine cannot lease a LinkedIn job until the gap elapses — **never two concurrent automated sessions**, and the combined rate stays under the ~20/day per-account cap regardless of how many owner machines are eligible (concrete SQL in §3). This is what lets v1's "exactly one machine" relax to "one IP, N machines" safely. **LinkedIn *discovery* (RF2/§8.5) is separately governed by `board:linkedin`, runs UNAUTHENTICATED, and is likewise single-owner-IP and carefully paced.**
> **Changed from v1 (flagged deliberately).** v1 §6 stated LinkedIn's rolling-24h cap "stays home-side and **never enters the fleet governor**." v2 reverses that: R1 needs a **fleet-side mutex**, so LinkedIn now lives in the governor (the `account:linkedin` row) **in addition to** the home-side rolling-24h cap in `launcher.py`, which remains as belt-and-suspenders. This is an intentional design change, not an oversight (also noted in §0.1 and the §19 risks).

**Outcome-aware adaptive circuit-breaker (R6).** Outcome counters increment atomically with `write_result` (per §3 semantics): `success_24h` (confirmed apply / clean scrape), `captcha_24h` (wall), `block_24h` (hard block / **scrape-block** for `board:` scopes), plus the generated `challenge_rate`. **Key insight: a rising per-IP captcha/block rate — or per-board scrape-block rate — is a LEADING INDICATOR that the IP/board is being flagged** — before a hard block. So:
- **Auto-throttle / pause:** when a `home_ip:<ip>`, `host:<domain>`, **or `board:<name>`** `challenge_rate` crosses a threshold, set `breaker_state='throttled'` (widen `min_gap_seconds`, drop `daily_cap`) or `breaker_state='paused'` with a `breaker_until` recovery time, so the IP/board **cools off and RECOVERS before a hard block**. The claim queries refuse leases (apply OR search-task) while `breaker_state != 'ok'`.
- **Auto-demote on hard block / CF-on-everything:** an IP that hits a hard block or CF-walls everything is set `breaker_state='demoted'` → it is removed from the apply role and **kept as PURE-COMPUTE-ONLY** (pure compute has no IP sensitivity), and an alert fires. (A discovery-only scrape-ban demotes that IP *for that board's discovery*, not necessarily for applies — the breaker is per-scope.) The machine stays useful; the IP cools off.
- **Learn captcha-heavy hosts / block-heavy boards:** hosts with persistently high `challenge_rate` are **routed to the owner machine** (where the owner can solve locally) or deprioritized in the ORDER BY; boards with persistently high scrape-block rates get a longer cadence / wider gap, or are routed to the owner IP.

**Safe per-job handling under a wall (R6).** A job that hits a wall is **parked in a frozen `challenge_pending` lease-hold** (not released, not double-claimable, not lost). The worker **MOVES ON immediately — it never blocks on a human.** Re-attempt is gated by the §9.1 double-apply check. Backoff is **bounded:** `defer → K retries → failed:human_unavailable → owner review`, **never infinite-retry**. A *scrape-blocked* search task is the discovery analogue: it re-queues to a different IP, bounded by `attempts` (RF3). **Fail-safe:** if detection is uncertain, **treat it as a wall and PARK — never guess or blind-submit** (a blind submit on a captcha page burns the IP).

**Integrity.** Because the cap/breaker check and the lease are one statement under `SKIP LOCKED`, two workers cannot both slip past the last unit of a cap or sneak past a tripped breaker — the row lock serializes the decrement-of-headroom. A nightly job rolls `window_start`, resets `count_24h` and the outcome counters (`success_24h`/`captcha_24h`/`block_24h`), and clears expired `breaker_until` back to `ok` — for `host:`, `home_ip:`, `account:`, and `board:` scopes alike.

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
On any human-needed wall, the worker screenshots the page (stored as a **broker-served blob**, referenced by `screenshot_url` — E2), writes the `auth_challenge` row (`kind`, `route`, `screenshot_url`, `worker_id`, `home_ip`), and sets the job to a **frozen `challenge_pending` lease-hold** (lease not released → no other machine grabs it). **No human present:** after a challenge timeout the worker marks the challenge `deferred`, releases the lease, and the job re-queues with `defer_until = now()+6h`. After K defers → `failed:human_unavailable`, surfaced in the dashboard for an owner decision. Bounded, never infinite (§6). **No technical detail of any of this is ever shown to a friend (§16.5)** — challenges route to the owner; the friend's Helper UI just keeps showing "Running ✓."

### 7.4 Gmail auth-code RELAY (R4)
All machines need email verification codes, but the **Gmail OAuth token (`gmail.readonly` = the WHOLE inbox) is NEVER distributed** to friends' machines. The token lives on **ONE trusted spot** — the owner machine or the broker.
- **Flow:** a worker hitting an `email_otp` wall calls the broker `request_otp(sender_hint, url, ts)`. The broker reads Gmail (reusing the **`gmail_outcomes` parser**), extracts **ONLY the verification code** for the matching ATS/sender from the **last ~60s**, and returns just that code **over the RPC response** — the code is **never persisted** (E1). The worker enters it and proceeds.
- **Disambiguating concurrent codes:** match by **sender + timestamp window**, write an `otp_request` bookkeeping row (`sender_hint`, `matched_email_ts`), and **mark-consumed** (`consumed_at`) so the same email isn't matched for two workers. If multiple workers are mid-OTP, the sender_hint + window + consumed flag keep them separate.
- **Friends get CODES, never email.** The broker returns a bare string over RPC; the inbox itself never leaves the trusted spot, and no code is stored.

---

## 8. Compute Distribution (PURE compute only — RF2)

Scoring / audit / triage / tailor / enrich-text have **no captcha and no IP sensitivity** — they are **genuinely IP-free** — so they ride the same lease pattern on `compute_queue` across cloud + residential idle CPU. **(RF2 correction: v2 wrongly lumped *discovery* into this plane. Discovery is network + IP/account-sensitive and is NOT here — it is its own governed lane, §8.5. This section is now strictly PURE compute.)**

- **PUSH:** `push_compute_jobs()` enqueues rows needing a compute task (`task='score'|'audit'|'tailor'|'enrich'`) with the minimal `payload` JSONB they need and an `est_cost_usd`.
- **Lease:** identical `FOR UPDATE SKIP LOCKED` claim (no apply-governor gate, no `board:` gate — nothing IP-sensitive to throttle for pure compute) — but **gated by the cost cap (§13):** refused when the rolling daily/total LLM spend in `llm_usage` exceeds `fleet_config.cost_cap_*`. Cloud workers (proxied, any IP — fine for pure compute) and residential idle workers compete freely. **Compute nodes also heartbeat (§11)** and report `spend_today_usd`, so the dashboard governs and monitors them too.
- **Write-back:** result JSONB lands in `compute_queue.result`; each call writes an `llm_usage` row (§13). `pull_results()` ingests results into the brain as **advisory** rows (`research_fit_score`, `research_decision`, tailored-resume refs) — never auto-promoted to `fit_score`/`audit_score`; the owner promotes explicitly (unified-brain rule). Tailored resumes are stored as assets and referenced by URL, respecting the "apply as-is unless owner opts in" rule.

This is the cheapest stage to scale first (S1) because it's the lowest-risk — no account, no IP, no human. **Pure compute costs real API money**, so it is the one plane the **cost governor (§13)** bounds. (Discovery, §8.5, has a *different* cost profile: no LLM spend, but real IP/scrape-block risk — hence its own governor, not the cost cap.)

---

## 8.5. Distributed Search-Task Scheduler (new — RF3; the discovery engine)

Discovery (RF2) is driven by a **scheduled, recurring search-task queue** that distributes the search space across the fleet. It is the discovery analogue of the apply queue — same lease pattern — with one key twist: **searches RECUR.**

### 8.5.1 Decomposing the search config into tasks
The owner's search config (`searches.yaml`: queries × boards × locations, served centrally from `fleet_config.search_config`) is **expanded automatically into discrete `search_tasks`** — **one task ≈ (query × board × location)** plus any extra facets in `params`. The config stays **centralized** (broker-served / `fleet_config`); workers never edit it. When the owner adds a query or a location, the expander mints the new tasks; when the owner prunes a search, the tasks are disabled (`enabled=false`). This keeps a single source of truth while spreading the *work* across 12 machines.

### 8.5.2 Claim → scrape → push (deduped)
Machines **CLAIM** due tasks via the §3 search-task claim (`FOR UPDATE SKIP LOCKED` on `status='queued' AND next_due_at <= now()`, governed by the `board:` scope). The scraper runs the **unauthenticated** search (RF2 — never logged into the apply account), extracts postings, and **pushes found postings via `push_postings` into the coordination layer (Topology A) / brain (Topology B)** — workers never write the brain directly in Topology A; postings land in coordination-PG and PULL into the SQLite brain (§2). Postings are **deduped by `dedup_key = (company, normalized_role)`** (§9.1) so the same posting found by two boards/two machines collapses to one. New postings enter the normal score → qualify → approve (§9.3) pipeline. **12 machines split the search space; each covers a DIFFERENT slice** (each claims different tasks), so the fleet covers far more ground before per-IP rate-limits bite than one machine could.

### 8.5.3 The key twist: searches RECUR
Each task carries a **refresh cadence** (`next_due_at`, `cadence_seconds`, e.g. re-run every ~4h). On a clean run the worker reschedules the task `cadence_seconds` into the future; it becomes **claimable again only when due**. This makes it a **DISTRIBUTED SCHEDULER, not a one-shot queue** — discovery stays fresh (new postings surface every cadence) **without 12 machines constantly re-running everything**. The `ORDER BY next_due_at ASC` claim gives oldest-due-first fairness, so coverage rotates evenly. (The `task_id` PK is stable across cadence cycles — the row is updated in place, not re-minted — see §3.)

### 8.5.4 Governed (RF2)
The search-task claim is gated by the per-`board:<name>` scrape governor + IP circuit-breaker (§6): per-board scrape min-gap, per-board daily scrape cap, and the adaptive breaker that treats a rising scrape-block rate as a leading indicator. **LinkedIn-search tasks** (`board='linkedin'`) get the careful treatment — **unauthenticated, single-owner-IP, spread, paced** — never on the apply account.

### 8.5.5 Resilient
A **scrape-blocked task** → its short lease expires (or the worker releases it) → it **re-queues to a DIFFERENT machine/IP** and retries on the next due window (`attempts` bounds the streak; `consecutive_blocks` feeds the `board:` breaker). **Coverage is not lost** when one IP gets blocked on one board — a different residential IP picks the task up.

### 8.5.6 Payoffs + dashboard coverage view
- **Broad coverage:** 12 machines >> 1 before rate-limits bite.
- **No duplicate work:** each task is claimed **once per cadence** (`SKIP LOCKED` + recurrence), and postings are deduped by `dedup_key`.
- **IP-spread:** each IP runs *fewer* searches → fewer per-IP scrape blocks (this is exactly why distributing discovery across residential IPs *helps*, RF2).
- **Coverage view on the dashboard (§11):** which searches are **live / last-refreshed / new-jobs-per-board**, surfaced so the owner can **prune dead searches** (`enabled=false`) that return nothing and reallocate cadence to productive ones.

---

## 9. Apply Quality — Dedup, Answer Bank, Calibrated Approval Policy (R9, R10, R11; RF1)

The fleet must apply *well*, not just *much*. Three first-class subsystems, all served/enforced centrally (never decided on a friend's box).

### 9.1 Distributed double-apply + cross-board dedup (R9)
- **Pre-submit guard:** before submitting, the worker does a **fast `applied_set` check** in Postgres against a shared applied-set, so **12 machines + retries + the sync gap can't double-apply.** The lease query already excludes already-applied postings (§3); the pre-submit check is the second line in case a concurrent apply landed during the lease window. On confirmed apply, the same transaction UPSERTs `applied_set`.
- **Posting-level (not URL-level) dedup:** the *same company+role* often appears on LinkedIn **and** Greenhouse **and** the company site. URL-level dedup still sends 3 applications to one employer (reads as spam). So we collapse by **`dedup_key = (company, normalized_role)` across boards** and apply **ONCE**. **(This is also the dedup target for discovered postings — §8.5 — so the search scheduler never floods the brain with the same posting found on three boards.)**
- **`normalized_role` definition (D4).** `normalized_role` = lowercase; strip seniority decorations to a canonical level token (`jr|sr|staff|principal|lead`), strip Roman/Arabic level suffixes (`II`, `III`, `2`), strip location/req-id/parenthetical noise, collapse synonyms via the existing role-family map (e.g. "Quantitative Developer" ≈ "Quant Dev"). `company` is canonicalized via the existing company-normalization (legal-suffix strip, known-alias merge). The pair is hashed to `dedup_key`. **Where it's computed:** in PUSH **and at discovery push-time (§8.5)** for Topology A; as a generated/trigger-maintained column on the brain's apply-eligible view for Topology B (no PUSH) — see §2. **(Note: this `dedup_key`/`normalized_role` machinery is needed by the discovery scheduler at S1.5, before the apply lane at S2 — it is pulled forward into S1.5's scope; see §17.)**
- **`got_response` semantics (D3 — clarified).** The lease/PUSH dedup guard excludes **any** posting already in `applied_set`, regardless of `got_response`. `got_response` is therefore **not** an additional lease-time filter; it is a **display/feedback flag** (§10) used to (a) mark "we already heard back" in the dashboard, and (b) short-circuit any *manual* re-queue attempt the owner might make on an already-answered posting. The lease query's existing `NOT EXISTS (applied_set)` already prevents re-apply; `got_response` does not re-gate it.

### 9.2 Screening-question ANSWER BANK (R10)
- **Centralized + synced, served by the broker (`get_answer`), never guessed locally.** Reuses the **existing question-bank**. Handles custom ATS questions: *years of X, work authorization, salary expectation, "why us", EEO/demographic*.
- **Fail-safe:** an **UNKNOWN question DEFERS to the owner** — the worker **never guesses**, because a wrong answer to a screening question = **instant auto-reject**. The job parks (like a captcha) with `failed:unknown_question` → owner answers it once → the answer is added to `answer_bank` (`status='known'`) → all future workers get it. The bank thus learns from each deferral.

### 9.3 Apply-APPROVAL — CALIBRATED WIDE-NET POLICY (R11; rewritten — RF1)

v2's gate was "above a threshold OR an explicit batch." v3 replaces it with a **calibrated approval POLICY** (`fleet_config.approval_policy`, JSON). The owner chose a **WIDE net** — but the width comes from **the system confidently qualifying many jobs, NOT from lowering the bar.** Selectivity is **AUTOMATED**, not abandoned. There are three parts: an **auto-approve band**, a **gray-zone review queue**, and a **post-hoc sampling audit**.

**A. Auto-approve band — anchored on "high-CONFIDENCE-qualified," NOT a raw score threshold.**
The auto-rule is **multi-criteria**, not `score ≥ 7`:
- **Strong fit** (`min_fit`, e.g. 7 — reads `COALESCE(audit_score, fit_score)`) **AND**
- the **qualification system's CONFIDENT, QUALIFIED verdict** (`min_confidence = qualified_confident`). **Source binding (load-bearing):** `min_confidence` reads the **qualification/triage system's confidence-bearing verdict field** in the brain — the same triage system named in *Builds on* that produces a *qualified / not-qualified* decision **together with a confidence level** (confident / borderline). The band keys on *that field*, not on the raw score: the triage system must say *qualified* **and** say so *confidently*. (This is the assumption the wide net rests on — see the §19 risk.) **AND**
- **NO red-flags** (`exclude_flags`): not a stretch / pivot-penalty case; location, work-authorization, and compensation deal-breakers all clear.

Jobs meeting the auto-rule get `approved_batch` **stamped AUTOMATICALLY — no manual click** (`approval_source='auto_band'`). **Default WIDE:** the owner trusts the qualification system at scale, so the auto-rule is permissive *in volume* precisely because the system confidently qualifies many jobs — the bar itself is not lowered. This is what makes the net wide.

**B. Gray-zone review queue — the owner attends ONLY the uncertain slice.**
Borderline / low-confidence / flagged jobs (within `gray_zone` bounds: `fit_lo..fit_hi`, or `confidence_lo='borderline'`, or carrying an excluded flag) go to a **fast batch review** on the dashboard (§11). The owner reviews **only this uncertain slice — not the whole pile**; releasing a batch stamps `approved_batch` with `approval_source='gray_zone'`. Jobs that are neither auto-approved nor released stay un-pushed.

**C. Sampling audit — NOT a gate; the PRIMARY human oversight under wide auto-flow.**
A **random N%** (`approval_sampling_rate`, e.g. 5%) of what **ACTUALLY WENT OUT** is flagged (`audit_sampled=true`) and surfaced for the owner's **post-hoc spot-check**. This is the **primary human oversight** when the auto-band is doing most of the approving: it **catches drift AFTER the fact** instead of gating every job up front. If the spot-check reveals the auto-band is letting through jobs the owner wouldn't have picked, the owner tightens the band (it's the same dial as A).

**D. Outcome-tuned — the owner moves the auto-bar with DATA.**
The §10 (R8) conversion data **validates and corrects the band**: where conversion is good, **widen**; where it sags, **tighten**. The dial is free to move in both directions — the owner calibrates the auto-rule (`min_fit`, `min_confidence`, `exclude_flags`) against real interview-rate signal, not a guess. The band is **outcome-validated**, not static.

**E. Wide approval ≠ fast blasting — the governors still PACE the sends.**
A wide approved pool does **not** mean a fast blast. The §6 **rate governors, per-host gaps, daily caps, and IP-protection still PACE the actual sends**, so a wide approved pool **drains at a SUSTAINABLE rate.** Account/IP safety is **identical whether the approved pool is 50 or 5,000** — a bigger pool just means the fleet works through it **over more days**, at the same polite per-host/per-IP rate. Approval width and send rate are **orthogonal**: approval decides *what is eligible*; the governor decides *how fast it goes out*.

**Why this is NOT "auto-blast everything ≥7."** The bar is the owner's **calibration** (multi-criteria, not a raw score), it is **confidence-gated** (the qualification system must be *confident*), it is **red-flag-excluded** (deal-breakers and stretch/pivot cases are held back), it is **outcome-validated** (the band moves with conversion data), and it is **sample-audited** (a random slice of sends is spot-checked for drift). Selectivity is **AUTOMATED**, not indiscriminate. Compute results remain **advisory**; the apply queue remains **owner-policy-gated**. (This preserves v2's guarantee that the fleet "NEVER auto-blasts everything scored ≥7" — now re-argued as an automated, multi-criteria, audited policy.)

**Mechanics & schema.**
- `fleet_config.approval_policy` (JSON): `default_width`, `auto_rule` (`min_fit`, `min_confidence`, `exclude_flags`), `gray_zone` (bounds). `fleet_config.approval_sampling_rate` (e.g. 0.05). (The scalar `approval_threshold` from v2 is REMOVED — the policy JSON replaces it.)
- The auto-rule evaluation runs at PUSH-time (Topology A) or as the apply-eligible view's predicate (Topology B): rows matching the auto-rule are auto-stamped `approved_batch` (`approval_source='auto_band'`); gray-zone rows are left un-stamped and surfaced for owner action; manual releases stamp `approval_source='gray_zone'`.
- At apply-time, `write_result` flags a random `approval_sampling_rate` fraction of confirmed applies `audit_sampled=true` for the §11 spot-check panel.
- The lease query predicate is unchanged: `approved_batch IS NOT NULL` (§3) — the policy simply controls *who/what* gets stamped, automatically or via the gray-zone release.

---

## 10. Outcome Tracking + Feedback Loop (R8)

This closes the loop from "applied" to "got a result" — the actual goal — **and feeds the approval policy's calibration dial (§9.3-D).**

- **Wire the Gmail scanner into the fleet.** The existing `inbox_events` scanner (read-only `gmail.readonly`) runs **on the trusted spot — the owner box or the broker, the same place the OTP relay lives (§7.4), never a friend box.** It classifies each application **RESPONSE** — *interview request / rejection / online-assessment request / acknowledgement* — and writes an `inbox_events` row keyed by `dedup_key` so it ties back to the exact application. (Workers never run the scanner and never touch the inbox.)
- **Flows back to the brain.** On the 60-sec PULL (Topology A) or directly into the brain PG (Topology B), outcomes land tied to the application, and set `applied_set.got_response = true`.
- **SURFACE INTERVIEW REQUESTS LOUDLY (the win).** Interview-request events are pinned at the top of the dashboard (§11) — **never buried under apply noise.** From there **the human takes over; interviews are never automated.**
- **Never re-apply to a job that already got a response.** The posting is already in `applied_set` (so the §3 dedup guard already blocks re-apply); `got_response=true` additionally flags it as "heard back" for the owner and blocks any manual re-queue (§9.1, D3).
- **Learn what converts → feed scoring AND the approval band (RF1).** Aggregate which **sources/roles actually CONVERT** (interview rate by board, by role family, by company tier) and feed that signal back into the **qualification/scoring** stage *and* into the **§9.3 approval-policy calibration**: where the auto-band's approved cohort converts well, widen the band; where it sags, tighten. The fleet gets **smarter, not just bigger.** (Advisory into the brain; the owner promotes scoring changes and moves the approval dial per the unified-brain rule.)

---

## 11. Fleet Health & Recovery + Centralized Dashboard (R7)

### 11.1 Heartbeat substrate
Every worker → broker **~every 20s** writes a `worker_heartbeat` row: `worker_id`, `machine_owner`, `roles` (any combination of apply/compute/discovery), `state` (idle/applying/searching/challenge-pending/paused), `current_job` (url or `task_id`), today's success/captcha/block counters, `spend_today_usd` (compute nodes), resource use (`cpu_pct`, `ram_pct`, `browser_count`), and **`sw_version` (R12 / §16.5)**.

### 11.2 Multi-signal STUCK detection (concrete thresholds — C5)
With heartbeat ~20s, apply/compute job timeout 900s, apply/compute lease TTL 1200s, discovery lease TTL 600s:
- **No heartbeat** for **> 90s** (≈ 4 missed beats) → dead/hung process or network partition.
- **Heartbeat alive but apply/compute job over max-duration:** `now() - job_started_at > 600s` (`max < 900s` job timeout, so stuck-detection fires *before* the reclaim sweep and they don't fight) → hung browser.
- **Heartbeat alive but DISCOVERY task over max-duration (RF3):** scrapes are quick, so discovery uses a tighter `now() - job_started_at > 300s` over-max threshold (well inside its 600s lease, leaving grace before reclaim) → hung scraper.
- **Crash-loop** (≥ 3 restarts within 10 min).
- **Resource-pegged** (cpu or ram sustained ≥ 95% across ≥ 3 consecutive beats).

### 11.3 Recovery
- **Per-machine WATCHDOG** auto-restarts a hung worker: **clean-kill the worker AND its Chromium children, free the CDP port (e.g. 9222), clear stale profile locks, relaunch.** (This automates exactly the orphaned-apply-Chromium / stuck-port-9222 cleanup the owner had to do by hand this session.)
- **REMOTE-RESTART from the dashboard:** the owner triggers a `remote_commands` row (`command='restart'`); the **friend does nothing** — the target machine's watchdog polls (`poll_commands`) and executes it locally.
- **POISON-JOB quarantine:** a job that crashes/hangs whoever claims it past K attempts is moved to `poison_jobs`, **pulled from the pool**, and **flagged for owner review** so it **stops taking out workers.**
- **STATELESS self-resume:** a restarted worker just resumes claiming — **no recovery state to rebuild**; the missing-heartbeat lease auto-reclaims (§5).
- **Non-technical-safe (RF4/§16.5):** all recovery is owner-side or automatic. **No technical error is ever surfaced to a friend** — the Helper's tray only ever shows `Running ✓ / Paused / "Jonathan needs a sec"`; everything else (errors, restarts, version pulls) routes to the **owner's dashboard**, who fixes it remotely.

### 11.4 CENTRALIZED DASHBOARD (single pane of glass)
Runs on the owner box or the broker, **reads Postgres**. It surfaces:
- **Per-machine** liveness / state / health (from `worker_heartbeat`).
- **Per-IP** captcha/block rates + **throttle/pause/demote** state (from `rate_governor`, §6) — for `host:`, `home_ip:`, `account:`, and `board:` scopes (§6).
- **Queue depth + applies-today-vs-caps** (apply/compute/LinkedIn).
- **SEARCH COVERAGE view (new — RF3/§8.5):** which searches are **live / last-refreshed / new-jobs-per-board**, so the owner can **prune dead searches** and reallocate cadence.
- **The CAPTCHA INBOX** (`auth_challenge` rows routed `owner_inbox`/`owner_tray`, §7) — the owner clears captchas here.
- **INTERVIEW REQUESTS, loud** (from `inbox_events`, §10) — the win, pinned.
- **The APPROVAL POLICY panel (RF1/§9.3):** the **auto-band stats** (how many auto-approved, the live `auto_rule`), the **gray-zone review queue** (the uncertain slice to attend), the **sampling-audit spot-check** (the random N% of *actually-applied* rows, `audit_sampled=true`), and a **calibration control** to widen/tighten the auto-bar against conversion data (§10).
- **The QUARANTINE** (`poison_jobs`) and **failures** (`failed:human_unavailable`, `failed:unknown_question`).
- **Cost** — fleet-wide LLM spend vs cap (§13).
- **ENROLL: "Add a machine" (new — RF4/§16.5)** — mints a personalized installer with the per-machine token baked in.

**ACTIONS:** restart a worker, pause a machine, clear captchas, **review the gray-zone batch / spot-check sampled-applied rows / tune the auto-band (§9.3)**, review failures, demote/restore an IP, **prune a dead search (§8.5)**, **add a machine (§16.5)**, push a **canary / fleet version (§14)**.

**ALERTS:** machine stuck, IP demoted, captcha-backlog spike, throughput stall, cost-cap approached, **board scrape-block spike (RF2/§8.5)**, **sampling-audit flag awaiting spot-check (RF1)**.

---

## 12. Consent + Data + Security

*(v1 §9, renumbered; deltas folded in.)*

- **What a friend installs:** the **ApplyPilot Helper app (§16.5)** — a thin tray app, nothing else. Zero-touch: a personalized installer (token baked in) the owner sends; the friend runs it and clicks "Allow/Start" once. Clear consent screen at install: *"This runs job applications as Jonathan from this machine. It stores his resume/contact info locally and no passwords. It only runs when your machine is idle (or in hours you set), pauses when you're using it, and you can pause or fully remove it anytime."* (§16)
- **What a friend holds:** `profile.json` + `resume.pdf` only, local, encrypted at rest (OS keychain-wrapped key). **No passwords, no LinkedIn, no brain, NO Gmail token, no other people's data.** Friends get **CODES from the relay, never the inbox** (§7.4). **Discovery scraping a friend's machine runs is UNAUTHENTICATED (RF2)** — no apply-account login ever lands on a friend box.
- **What a friend sees:** tray status + their own machine's resource caps/controls (§16). Friend machines run **no-wall applies only** and are **never nagged with captchas** (§7.2). They do **not** see the brain, other machines, aggregate data, or any technical error (§16.5). They do **not** see the inbox.
- **Securing the PG connection:** friends get a **per-machine bearer token to the API broker**, never a Postgres DSN. The broker holds the only DB credential **and the only Gmail token**, exposes a tiny RPC surface (lease/result/challenge/heartbeat/assets/get_answer/request_otp/get_config/poll_commands/report_usage/**lease_search_task/push_postings/get_search_config**), and rate-limits/audits per token. A leaked token can't dump the queue, touch the brain, or read the inbox — and it's revocable in one row. The token is **baked into the personalized installer (§16.5)** so the friend never types a credential.
- **Revocation / kill-switch:** (a) `fleet_config.paused = true` halts all leasing instantly (global kill). (b) Revoking a machine's broker token stops that machine alone. (c) Tray **Uninstall+wipe** removes assets and the worker. (d) Per-machine token rotation on a schedule. (e) **Remote pause/drain** from the dashboard (§11). Every apply is logged with `worker_id` + `machine_owner` for a consent audit trail.

---

## 13. Cost Governor (R14)

Compute (scoring/audit/tailor/enrich) costs **real API money**; apply is $0; **discovery is $0 in API spend but carries IP/scrape-block risk governed separately (§6 `board:` scope, RF2) — it does NOT touch the cost cap.**

- **Fleet-wide LLM-spend tracking:** every compute call writes an `llm_usage` row (worker, machine_owner, task, model, tokens, `cost_usd`) — reusing the existing `llm_usage` data shape. Each compute node also reports `spend_today_usd` in its heartbeat (§11). The dashboard shows **per-machine + total** spend.
- **Configurable spend cap — single home (E4):** `fleet_config.cost_cap_daily_usd` / `cost_cap_total_usd`. The **compute lease is refused** (the gate in §8) once the rolling daily or total spend exceeds the cap. The governor (§6) does not carry a parallel cost mechanism — it merely references this cap at the compute-lease point. Apply leases and discovery scrapes are $0 and unaffected by the cost cap.
- **Alerts** when spend approaches the cap (§11).

---

## 14. Fleet Auto-Update / Version Management (R12)

Twelve distributed machines must get fixes/features **without version skew** — and, for non-technical friends, **without reinstalls** (§16.5).

- **Broker serves the current pinned worker version** (`fleet_config.pinned_worker_version`); workers **self-update** by polling `get_config` / `poll_commands` and pulling the new package from the broker, then the watchdog relaunches. The **Helper app's auto-updater (§16.5)** does this transparently — the friend never reinstalls.
- **Staged rollout of updates:** **canary a new version on 1 machine** — `fleet_config.canary_version` + `canary_worker_id` target one worker, and the per-worker `self_update` command carries the target in `remote_commands.target_version` (D8) so the canary pulls exactly that build. **Watch its heartbeat/health/outcomes**, then promote **fleet-wide** by bumping `pinned_worker_version`. A bad update **can't break the whole fleet at once.**
- **Version is reported in the heartbeat** (`sw_version`, §11) so the dashboard shows skew and the canary's health side-by-side with the fleet.

---

## 15. Per-Machine Canary (R15)

A new machine, on joining, is **validated BEFORE it's trusted with live applies** — protecting against a misconfigured machine doing bad live applies. **The Helper app runs the canary AUTOMATICALLY at enrollment (§16.5)** — the friend does nothing; the dashboard shows "✓ Running" only after it passes.

- **SMOKE TEST:** reach the broker, fetch assets, drive a browser (launch Chromium, load a page), report a heartbeat. Confirms the install works end-to-end. **(For discovery-capable machines, the smoke test also claims a `search_tasks` row, runs the unauthenticated scrape, and confirms postings parse + dedup — but does NOT count the run against coverage. This validates the scraper + `board:` governor inline, without a separate named gate — RF3.)**
- **DRY-RUN apply:** claim a real job, **fill the form, but do NOT submit** — confirms profile hydration, the answer bank, and the captcha detector all behave on a live ATS.
- Only after these pass is the machine marked `validated` (capability flag) and allowed to claim **live** apply (and live discovery) leases. This **fits the staged rollout** (§17) as the gate every new machine passes.

---

## 16. Friend-Machine Stewardship (R13)

Being a good guest is what keeps friends running it.

- **Resource caps:** cap CPU / RAM / browser-count (`WORKERS=N`, §5) so a friend's laptop is **never pegged.** Defaults are conservative on laptops (≈2 browsers).
- **Run-on-idle:** the worker runs **only when the machine is idle**, or within **allowed hours** the machine's owner sets.
- **Auto-pause on activity:** detect **foreground user activity** and **auto-pause** immediately; resume when idle again. (Reported as `state='paused'` in the heartbeat.)
- **Friend control from the tray:** the friend can **set limits** (max browsers, allowed hours, CPU/RAM ceiling) and **see/control** the worker (pause, view today's count, uninstall+wipe).
- **Invisible by default (RF4):** run-on-idle + auto-pause-when-the-friend-is-using-the-machine + conservative resource caps are **all ON by default** (§16.5). The Helper quietly helps in the background and never bugs the friend.

---

## 16.5. Fleet Worker App & Onboarding (new — RF4)

The fleet runs on a **mixed Windows + Mac** group of **non-technical friends.** The worker is therefore delivered as a **thin, purpose-built "ApplyPilot Helper" app** with **zero-touch** enrollment and a **non-technical-safe** operating model. Ties to §5 (worker), §12 (consent), §15 (canary), §16 (stewardship), §14/R12 (auto-update).

### 16.5.1 A thin Helper app — NOT the full Python tool
The friend installs a small, purpose-built **"ApplyPilot Helper"** desktop app — **not** the full Python research/brain tool. The Helper **WRAPS the existing worker logic (`container_worker.py`) + bundled Chromium** in a small desktop shell with a friendly **tray UI**, and **talks ONLY to the broker** (the same RPC surface as §5/§12). The **heavy brain / Python research tree stays on the owner's machine** — the friend's box only ever runs the thin worker.
- *Implementation note (a plan detail, not over-specified here):* bundle the Python worker via PyInstaller/Nuitka + a cross-platform tray + an auto-updater. Cross-platform: **Windows + Mac.**

### 16.5.2 Zero-touch enrollment (non-technical)
- Owner clicks **"Add a machine"** on the dashboard (§11) → the broker mints a **personalized installer with the per-machine token BAKED IN** → the friend **downloads + runs + clicks "Allow/Start" ONCE.** **No code-typing — the link IS the enrollment.**
- On first run the Helper **self-configures from the broker**: pulls its token-scoped config (assets, resource caps, pinned version), registers its capability flags, and **runs the canary (§15) automatically.** Then it shows **"✓ Running."** The friend did nothing but click once.

### 16.5.3 Two config layers
- **Owner sets FLEET config centrally** (broker / `fleet_config`): governor caps, approval policy (§9.3), search config (§8.5), pinned version, cost cap.
- **Friend sets THEIR machine prefs in the tray** (§16): allowed hours, resource caps (max browsers / CPU / RAM), auto-pause. The two layers never conflict — fleet config governs *what the fleet does*; the friend's prefs govern *when/how hard their machine participates.*

### 16.5.4 Maintenance-free + non-technical-safe
- **Auto-update (§14):** the Helper pulls new versions transparently — **no reinstalls.**
- **Auto-restart via the watchdog (§11 / R7):** a hung worker is cleaned up and relaunched locally.
- **NO technical error is EVER shown to the friend.** All errors + recovery route to the **OWNER's dashboard.** The friend only ever sees **`Running ✓` / `Paused` / `"Jonathan needs a sec"`** (the last is rare). If something breaks, the **owner fixes it REMOTELY**; the friend does nothing.

### 16.5.5 Invisible by default (stewardship §16)
**Run-on-idle + auto-pause-when-the-friend-is-using-the-machine + conservative resource caps are all ON by default.** The Helper quietly helps in the background and **never bugs them.** A friend who forgets it's installed should still be a perfect host.

### 16.5.6 Install signing — manual-walkthrough-FIRST, notarization-WHEN-READY (owner's call)
Mac is the hard case. On current macOS an unsigned app pushes the user into **System Settings → Privacy & Security → "Open Anyway,"** and an unsigned **`.pkg` is blocked harder than a `.app`.** Therefore:
- **Ship a `.app`, NOT a `.pkg`** (the `.app` is more bypassable), and the Helper's **first-run screen shows the exact click-path for the user's macOS version** (a consistent, version-aware walkthrough so a non-technical friend can get past Gatekeeper without help). *(This supersedes the earlier v2 "signed `.pkg`" footprint line — §5 now ships a `.app`, unsigned-first.)*
- **Windows unsigned** is the easy case: SmartScreen's **"More info → Run anyway."** The first-run screen shows that path too.
- **Apple notarization ($99 Apple dev account, ~1–2 days) + Windows code-signing are set up IN PARALLEL** — they **do NOT block starting.** The owner hand-walks the first one or two installs; **flip to notarized/signed installers BEFORE scaling to the full group** so the owner isn't hand-walking all twelve. **Notarization is the near-term goal, not a launch blocker.**

---

## 17. Staged Rollout

Each stage is independently shippable and reversible.

- **S0 — Pick topology (R2).** Default **local SQLite** (Topology A). Stand up the broker + Postgres coordination schema (§3). (Flip to **cloud Postgres-as-brain**, Topology B, later when fleet scale justifies it; that's a config flip + one-time 77k-job migration and the sync bridge disappears.)
- **S1 — Distribute PURE compute only (zero apply risk).** Add `compute_queue`, `push/pull`, the broker, the **cost governor + `llm_usage`** (§13), and the **heartbeat substrate + dashboard skeleton** (§11). Run pure-compute workers on the owner box + 1 cloud node. (Apply-PUSH is not run yet — no `approved_batch` exists until S2; see the §2 rollout note.) *Ship gate:* advisory scores flow back, no brain corruption, spend tracked and capped, heartbeats visible.
- **S1.5 — Governed discovery scheduler (new — RF2/RF3).** Add `search_tasks` + the `board:<name>` governor scope + the search-task claim + `push_postings` + the dashboard **search-coverage view** + the **`dedup_key`/`normalized_role` machinery (§9.1, pulled forward into this stage — discovery needs posting-level dedup to land postings without flooding the brain)**. Expand `searches.yaml` into recurring tasks; run **unauthenticated** discovery on the owner box + 1 residential IP first, then spread. *Ship gate:* found postings dedup into the brain by `dedup_key`; per-board min-gap/cap hold; a simulated scrape-block trips the `board:` breaker and re-queues the task to a different IP; LinkedIn-search runs unauthenticated, single-owner-IP; coverage view shows live/last-refreshed/new-per-board.
- **S2 — One residential apply worker (owner's own second machine).** Add governor apply scopes (now **outcome-aware** §6, incl. the per-IP breaker join §3) + governor-gated claim + `auth_challenge` **detector/router** (§7) + the **answer bank** (§9.2) + the **calibrated approval policy** (§9.3: auto-band + gray-zone + sampling) + `applied_set` **dedup** (§9.1, the apply-side guard on the machinery landed in S1.5) + the **Gmail OTP relay** (§7.4) + the **watchdog** (§11) + the **Helper app + zero-touch enroll** (§16.5). New machines pass the **canary** (§15). Run a tiny daily cap (global 10/day). Start the auto-band **narrow** to validate, then widen as confidence builds. *Ship gate:* applies succeed on residential IP; the auto-band auto-stamps `approved_batch` with no click; gray-zone surfaces only the uncertain slice; the sampling audit flags a random N% of actual sends for spot-check; captchas route to the owner inbox/tray; OTPs auto-solve via relay; unknown questions defer; governor caps + per-IP breaker hold; watchdog recovers a killed worker; the Helper enrolls from a baked-in-token installer in one click.
- **S3 — Add 1–2 friend machines, then scale to ~10–12.** Per-machine tokens (baked into installers, §16.5), consent flow, **friend stewardship** (idle/auto-pause/caps, §16), per-home caps, **WORKERS=N auto-sizing** (§5), **discovery spread across friend IPs** (RF2 — each IP runs fewer searches). Captchas on friend machines **bounce to the owner inbox** (friends never nagged; no technical error ever shown, §16.5). Raise the global cap and **widen the approval auto-band** gradually while watching per-IP `challenge_rate`, per-board scrape-block rate, and the **adaptive breaker/demote** (§6). **Flip to notarized/signed installers (§16.5) before the full group.** *Ship gate:* no IP shows a rising challenge-rate or scrape-block trend; demote/throttle works; consent/audit logging complete; canary passes per machine; the owner hand-walks ≤2 installs before signing lands.
- **S4 — Cloud compute scale-out.** Add proxied cloud **pure-compute** nodes to `compute_queue` (proxied/any IP fine — pure compute is IP-free; discovery stays on residential IPs, RF2). *Ship gate:* compute throughput up under the cost cap; apply + discovery lanes unaffected.
- **S5 — Outcome loop live (R8).** Wire the `inbox_events` scanner (on the trusted spot) → outcomes into the brain and the dashboard; **interview requests surface loudly**; conversion signal feeds scoring **and the approval-band calibration (§9.3-D)**; no re-apply after a response. *Ship gate:* a real response ties to its application; an interview pins to the top; an answered posting is excluded from re-apply; the auto-band dial visibly moves with conversion data.
- **S6 — LinkedIn lane (last, owner-only, ONE IP / N machines — R1).** Wire `linkedin_queue` + the `account:linkedin` mutex (§3 LinkedIn lease) as **multi-consumer but single-IP, governor-serialized**; keep the home-side rolling-24h cap. (LinkedIn *discovery* was already live, unauthenticated, from S1.5.) *Ship gate:* LinkedIn applies only ever originate from the one owner IP; never two concurrent sessions; combined rate under the per-account cap.
- **S7 — Auto-update + topology flip (optional).** Turn on **staged self-update** (§14: canary → fleet) via the Helper's auto-updater (§16.5). If/when scale justifies it, execute the **A→B cloud-brain migration** (§1.5).

---

## 18. (reserved — see §19 for Risks)

*(Section numbers preserved from v2: v2's Risks section was §18. v3 inserts §16.5 and renumbers Risks to §19 to keep the new section adjacent to stewardship. The cross-references in §0.1 and §6 that said "§18" now read "§19." The content below — §19 — is the v2 Risks + Open Questions, with the four v3 refinements' risks folded in.)*

---

## 19. Risks + Open Questions

- **Double-apply across machines.** Mitigated by **posting-level (`dedup_key`) dedup** + the **pre-submit `applied_set` guard** (§9.1) + `crash_unconfirmed` pinning, but a confirmed-but-unsynced apply could in theory be re-leased after a long Postgres/home partition (**Topology A only**; Topology B has no sync gap). *Largely closed by R9; residual risk only in long-partition Topology A.*
- **Governor accuracy under clock skew / reclaim.** The cap counter increments on confirmed apply (or scrape run); a job that succeeds but whose `write_result` is lost (worker dies post-submit) under-counts both the cap and the outcome counters. Acceptable (errs toward politeness) but worth monitoring; the adaptive breaker tolerates noise via *rates* not absolutes. (With the v2 outcome counters, walls/blocks DO increment — the under-count risk is only the lost-`write_result` edge.)
- **Adaptive-breaker threshold tuning (now also `board:` — RF2).** The `challenge_rate` thresholds for throttle/pause/demote are initial guesses **for both apply hosts/IPs and discovery boards.** *Open:* learn them per host/IP/**board** from observed block-precursors; avoid flapping (hysteresis via `breaker_until`).
- **Discovery scrape-block dynamics (new — RF2/§8.5).** Per-board scrape rate-limits and ban thresholds are unknown and board-specific; LinkedIn unauthenticated scraping is the riskiest. *Open:* instrument per-board scrape-block rates; tune `board:` `min_gap`/`daily_cap`/cadence; confirm that residential IP-spread actually reduces per-IP scrape blocks as hypothesized; verify LinkedIn unauthenticated discovery from the single owner IP doesn't endanger the *apply* account.
- **Search-coverage vs. freshness vs. politeness (new — RF3).** Cadence (`cadence_seconds`) trades freshness against scrape volume; too tight wastes scrape budget and risks blocks, too loose misses fresh postings. *Open:* per-board adaptive cadence (lengthen where `new_count` is consistently low; shorten hot boards); prune dead searches (`enabled=false`) automatically when `new_count`≈0 over many runs.
- **Approval-band calibration & drift (new — RF1).** The wide auto-band trusts the qualification system's *confident-qualified* verdict; if that verdict is miscalibrated, a wide band auto-applies to jobs the owner wouldn't pick. *Mitigated* by the gray-zone queue (the uncertain slice still gets human eyes) and the **sampling audit** (post-hoc drift detection) — but the audit is *after the fact*, so some off-target sends happen before the owner tightens. *Open:* right sampling rate vs. owner review load; how fast conversion data (§10) can move the dial without overfitting; ensure the "confident-qualified" verdict field the band keys on (§9.3-A) is itself well-calibrated (this is the load-bearing assumption of the wide net).
- **Approval throughput is no longer the bottleneck — but audit attention is (new — RF1).** v2's worry was that per-job owner gating starves the fleet. RF1 removes that (auto-band needs no clicks). The *new* bottleneck is the owner's **gray-zone + sampling-audit attention.** *Open:* keep the gray-zone slice small (good calibration) and the audit batch fast, else the wide net's oversight lapses.
- **Friend-machine trust.** A malicious friend could tamper with the local Helper. Data minimization caps the blast radius (resume + contact only; **no Gmail token, no passwords, unauthenticated discovery**), but *open:* should applies from a machine be signed/attested? The **canary** (§15) catches misconfiguration, not malice.
- **Broker as single point of failure (now also Gmail relay + answer bank + config + version + inbox scanner + asset server + search-config server + posting sink).** It's the only path to PG, the only holder of the Gmail token, and the answer/version/asset/search-config source; needs HA or fast restart. *Open:* run it on the owner box or a managed host? In Topology B it co-locates with the brain PG. The **watchdog** covers worker recovery, not broker recovery.
- **Gmail relay concurrency.** Disambiguation by sender + 60s window + mark-consumed (§7.4) could still mis-hand a code if two ATSes mail from the same sender within the window. *Open:* tighten with per-request nonces echoed in the OTP email where the ATS supports it.
- **Captcha binding & remote-assist.** Assumed captcha is bound to the browser session/IP that raised it (hence the bounce re-attempts on the *owner's* IP). Remote-assist (§7.2, deferred) would preserve the friend's session for binding. *Open:* verify per-ATS whether the owner's IP re-attempt succeeds vs. needing remote-assist.
- **Per-host caps & breaker are guesses.** Initial Greenhouse/Lever/Ashby caps, min-gaps, and breaker thresholds are unvalidated. *Open:* instrument per-host outcome rates and auto-tune `daily_cap`/`min_gap_seconds`/thresholds.
- **Answer-bank coverage vs. deferral load.** Early on, many questions are unknown → many owner deferrals (§9.2). Acceptable (the bank learns), but *open:* seed it aggressively from the existing question-bank to minimize cold-start deferrals.
- **Cost cap vs. throughput.** A tight LLM spend cap (§13) can starve compute. *Open:* per-task cost budgeting + prioritizing high-value scoring under the cap.
- **Helper app delivery & signing (new — RF4).** Until notarization/code-signing land, the owner hand-walks Gatekeeper/SmartScreen per install; the version-aware first-run walkthrough mitigates but doesn't eliminate friction. *Open:* land Apple notarization ($99, ~1–2 days) + Windows signing before the full group; confirm the `.app` (not `.pkg`) bypass path holds on each friend's macOS version; verify the auto-updater path works post-notarization without re-triggering Gatekeeper.
- **Helper "invisible by default" vs. coverage (new — RF4).** Aggressive auto-pause-on-activity + run-on-idle (good guesting) means a heavily-used friend laptop contributes little. Acceptable (account safety + friend goodwill dominate), but *open:* model expected fleet duty-cycle so caps/cadence assume realistic idle time, not 24/7.
- **Cloud-brain migration (Topology B).** Migrating schema + 77k jobs to PG, accepting a network dependency and data-in-cloud. *Open:* migration runbook + rollback to SQLite; verify `brainDb` PG target parity before the flip; confirm `dedup_key` generation parity in the no-PUSH path (§2/§9.1), including the discovery push path (§8.5).
- **Residential ToS / consent durability.** Friends may revoke informally (turn the machine off, or auto-pause keeps it idle). The defer/timeout + stewardship paths handle it, but *open:* define a clean "machine retired" state that drains its in-flight leases (`remote_commands: drain`).
