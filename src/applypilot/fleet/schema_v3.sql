-- ===========================================================================
-- Distributed residential fleet v3 schema (Postgres).
--
-- Layered ON TOP of apply/fleet_schema.sql (which creates apply_queue,
-- fleet_config, fleet_assets). Run fleet_schema.sql FIRST, then this file.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, CREATE ... IF NOT EXISTS, enum + value
-- guards. Safe to run on every broker/home startup. In Topology A these are
-- coordination tables; in Topology B they are brain tables (identical SQL).
--
-- See docs/superpowers/specs/2026-06-26-distributed-residential-fleet-design.md
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Extend apply_queue with residential + governance columns (R1, R6, R9, R11).
-- ---------------------------------------------------------------------------
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS worker_home_ip TEXT;   -- sending residential IP
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS target_host    TEXT;   -- effective apply host (governor key)
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS lane           TEXT NOT NULL DEFAULT 'ats';
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS dedup_key      TEXT;   -- (company, normalized_role) -- R9
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS approved_batch TEXT;   -- owner approval token -- R11

CREATE INDEX IF NOT EXISTS idx_apply_queue_dedup ON apply_queue (dedup_key);
CREATE INDEX IF NOT EXISTS idx_apply_queue_approved
    ON apply_queue (score DESC) WHERE status = 'queued' AND approved_batch IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Extend fleet_config (single-row id=1) with v3 controls (R11, R12, R14).
-- ---------------------------------------------------------------------------
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS approval_threshold     REAL;            -- auto-approve fit floor (NULL = batch-only)
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS approval_policy        JSONB;           -- {min_fit, min_confidence, exclude_flags[]} -- R11
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS approval_sampling_rate REAL NOT NULL DEFAULT 0.0;  -- audit sample fraction
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS cost_cap_daily_usd     NUMERIC(10,2) NOT NULL DEFAULT 0;  -- 0 = no cap -- R14
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS cost_cap_total_usd     NUMERIC(10,2) NOT NULL DEFAULT 0;  -- 0 = no cap
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS pinned_worker_version  TEXT;            -- R12 fleet version
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS canary_version         TEXT;            -- R12 staged update
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS canary_worker_id       TEXT;

-- ---------------------------------------------------------------------------
-- Status enum shared by the compute + search-task queues.
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    CREATE TYPE fleet_task_status AS ENUM (
        'queued',      -- eligible to lease (search: AND next_due_at <= now())
        'leased',      -- a worker holds it
        'done',        -- terminal success (compute: result written / search: run complete)
        'failed',      -- terminal failure
        'quarantined'  -- poison: pulled from the pool (R7)
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------------
-- compute_queue: score / audit / tailor / enrich -- IP-free, cost-governed (§8, R14).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS compute_queue (
    url               TEXT PRIMARY KEY,
    task              TEXT NOT NULL,                 -- 'score'|'audit'|'tailor'|'enrich'
    payload           JSONB,                         -- minimal context for the task
    status            fleet_task_status NOT NULL DEFAULT 'queued',
    lease_owner       TEXT,
    lease_expires_at  TIMESTAMPTZ,
    attempts          INTEGER NOT NULL DEFAULT 0,
    result            JSONB,                         -- advisory score/audit/tailored-resume ref
    est_cost_usd      NUMERIC(10,4) NOT NULL DEFAULT 0,
    synced_to_home_at TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_compute_lease ON compute_queue (status) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_compute_reclaim ON compute_queue (lease_expires_at) WHERE status = 'leased';
CREATE INDEX IF NOT EXISTS idx_compute_unsynced ON compute_queue (updated_at)
    WHERE status IN ('done','failed') AND synced_to_home_at IS NULL;

-- ---------------------------------------------------------------------------
-- search_tasks: distributed, RECURRING discovery (R3-disc / RF3).
-- One task = (query x board x location). Re-runs when next_due_at passes.
-- IP/account-sensitive: governed by board:<name> scope (RF2).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_tasks (
    task_id           TEXT PRIMARY KEY,              -- hash(query|board|location)
    query             TEXT NOT NULL,
    board             TEXT NOT NULL,                 -- 'indeed'|'linkedin'|'greenhouse'|... (governor key)
    location          TEXT,
    params            JSONB,                         -- results_wanted, hours_old, filters, ...
    status            fleet_task_status NOT NULL DEFAULT 'queued',
    lease_owner       TEXT,
    lease_expires_at  TIMESTAMPTZ,
    next_due_at       TIMESTAMPTZ NOT NULL DEFAULT now(),  -- claimable when <= now()
    cadence_seconds   INTEGER NOT NULL DEFAULT 21600,      -- re-run cadence (6h default)
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_run_at       TIMESTAMPTZ,
    result_count      INTEGER,                        -- postings found on last run (coverage view)
    last_error        TEXT,
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_search_due
    ON search_tasks (board, next_due_at) WHERE status = 'queued' AND enabled;
CREATE INDEX IF NOT EXISTS idx_search_reclaim ON search_tasks (lease_expires_at) WHERE status = 'leased';

-- ---------------------------------------------------------------------------
-- linkedin_queue: ONE owner IP, N coordinated machines, governor-serialized (R1).
-- Same shape as apply_queue (separate lane).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS linkedin_queue (LIKE apply_queue INCLUDING DEFAULTS INCLUDING INDEXES);

-- ---------------------------------------------------------------------------
-- rate_governor: outcome-aware + adaptive circuit-breaker (R6, R1, RF2).
-- scope_key examples:
--   'global' | 'host:boards.greenhouse.io' | 'board:linkedin'
--   'home_ip:1.2.3.4' | 'account:linkedin' (the LinkedIn mutex)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rate_governor (
    scope_key       TEXT PRIMARY KEY,
    window_start    TIMESTAMPTZ NOT NULL DEFAULT now(),   -- rolling 24h anchor
    count_24h       INTEGER NOT NULL DEFAULT 0,           -- CAP counter (++ on confirmed terminal)
    daily_cap       INTEGER NOT NULL DEFAULT 1000000,     -- per-scope cap (large = effectively none)
    last_applied_at TIMESTAMPTZ,                          -- per-scope min-gap / mutex stamp
    min_gap_seconds INTEGER NOT NULL DEFAULT 90,
    -- outcome counters (++ on their own terminal class -- the deliberate v2/v3 change):
    success_24h     INTEGER NOT NULL DEFAULT 0,
    captcha_24h     INTEGER NOT NULL DEFAULT 0,
    block_24h       INTEGER NOT NULL DEFAULT 0,
    challenge_rate  REAL GENERATED ALWAYS AS (
        CASE WHEN (success_24h + captcha_24h + block_24h) = 0 THEN 0
             ELSE (captcha_24h + block_24h)::real
                  / (success_24h + captcha_24h + block_24h) END
    ) STORED,
    breaker_state   TEXT NOT NULL DEFAULT 'ok',           -- 'ok'|'throttled'|'paused'|'demoted'
    breaker_until   TIMESTAMPTZ,                          -- auto-recover time
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Pristine min-gap, so a 'throttled' breaker can widen the gap (base * multiplier)
-- WITHOUT compounding across throttle->recover->throttle cycles, and restore it
-- exactly on recovery. NULL is treated as "base == current min_gap_seconds".
ALTER TABLE rate_governor ADD COLUMN IF NOT EXISTS base_min_gap_seconds INTEGER;

-- ---------------------------------------------------------------------------
-- llm_usage cost ledger (R14). The CAP lives in fleet_config; this is the spend log.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_usage (
    id            BIGSERIAL PRIMARY KEY,
    worker_id     TEXT,
    machine_owner TEXT,
    task          TEXT,
    model         TEXT,
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    cost_usd      NUMERIC(10,6) NOT NULL DEFAULT 0,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage (ts);

-- ---------------------------------------------------------------------------
-- applied_set: cross-board posting-level dedup / double-apply guard (R9).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS applied_set (
    dedup_key        TEXT PRIMARY KEY,               -- (company, normalized_role) -- board-agnostic
    company          TEXT,
    normalized_role  TEXT,
    first_applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_url      TEXT,                            -- the one URL we applied through
    got_response     BOOLEAN NOT NULL DEFAULT FALSE   -- set by the outcome loop (R8); display/feedback
);

-- ---------------------------------------------------------------------------
-- answer_bank: screening-question answers, broker-served, defer-on-unknown (R10).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS answer_bank (
    q_norm     TEXT PRIMARY KEY,                      -- normalized question key
    q_raw      TEXT,
    answer     TEXT,                                  -- owner-vetted answer (NULL while unknown)
    kind       TEXT,                                  -- 'work_auth'|'years_x'|'salary'|'eeo'|'why_us'|...
    status     TEXT NOT NULL DEFAULT 'unknown_deferred',  -- 'known'|'unknown_deferred'
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- auth_challenge: captcha inbox / human-in-the-loop (R3). Surrogate id PK so the
-- SAME url can wall on a friend box AND be re-attempted on the owner box (C4).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS auth_challenge (
    id             BIGSERIAL PRIMARY KEY,
    url            TEXT NOT NULL,
    worker_id      TEXT,
    machine_owner  TEXT,
    home_ip        TEXT,
    kind           TEXT,                              -- classifier: 'visible_captcha'|'email_otp'|'sms_otp'
                                                      --  |'login_gate'|'invisible_block'|'cf'|...
    route          TEXT,                              -- 'owner_inbox'|'owner_tray'|'skip'|'remote_assist'
    screenshot_url TEXT,                              -- broker-served blob ref (NOT inline BYTEA)
    raised_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at    TIMESTAMPTZ,
    outcome        TEXT                               -- 'solved'|'skipped'|'deferred'|'rerouted_owner'
);
CREATE INDEX IF NOT EXISTS idx_challenge_open ON auth_challenge (url) WHERE resolved_at IS NULL;

-- ---------------------------------------------------------------------------
-- otp_request: Gmail relay bookkeeping (R4). The CODE is NEVER persisted (E1) --
-- it is returned only over the broker RPC response. This row is the request +
-- consumed AUDIT TRAIL; single-delivery (not handing one code to two workers) is
-- enforced by the owner-side relay that matches+consumes the email, not by this row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS otp_request (
    id               BIGSERIAL PRIMARY KEY,
    worker_id        TEXT,
    url              TEXT,
    sender_hint      TEXT,                            -- ATS/sender domain the worker expects
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    matched_email_ts TIMESTAMPTZ,                     -- timestamp of the email the broker matched
    consumed_at      TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- inbox_events: outcome tracking / feedback loop (R8). Written by the inbox
-- scanner on the TRUSTED spot (owner box / broker), keyed by dedup_key.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inbox_events (
    id          BIGSERIAL PRIMARY KEY,
    dedup_key   TEXT,                                 -- ties response -> application (R9 key)
    url         TEXT,
    event_type  TEXT,                                 -- 'interview'|'rejection'|'assessment'|'ack'
    sender      TEXT,
    received_at TIMESTAMPTZ,
    raw_snippet TEXT,
    surfaced    BOOLEAN NOT NULL DEFAULT FALSE         -- interview requests surfaced loudly
);
CREATE INDEX IF NOT EXISTS idx_inbox_dedup ON inbox_events (dedup_key);
CREATE INDEX IF NOT EXISTS idx_inbox_unsurfaced ON inbox_events (received_at)
    WHERE event_type = 'interview' AND NOT surfaced;

-- ---------------------------------------------------------------------------
-- workers: enrollment / registration + capability flags + canary validation
-- (R4 token auth, R7 health, R15 canary, lane split §4).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workers (
    worker_id     TEXT PRIMARY KEY,
    machine_owner TEXT,
    token_hash    TEXT,                               -- sha256 of the broker bearer token
    public_ip     TEXT,                               -- registered egress IP (LinkedIn lane gate)
    capabilities  JSONB,                              -- {can_ats, can_linkedin, can_compute, can_discover}
    validated     BOOLEAN NOT NULL DEFAULT FALSE,     -- passed the canary (R15) -> may do live applies
    enrolled_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- worker_heartbeat: fleet health substrate (R5, R7). Updated ~every 20s.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS worker_heartbeat (
    worker_id       TEXT PRIMARY KEY,
    machine_owner   TEXT,
    home_ip         TEXT,
    role            TEXT,                             -- 'apply'|'compute'|'discover'|'both'
    state           TEXT,                             -- 'idle'|'applying'|'challenge_pending'|'paused'
    current_job     TEXT,
    job_started_at  TIMESTAMPTZ,                      -- over-max-duration detection
    success_today   INTEGER NOT NULL DEFAULT 0,
    captcha_today   INTEGER NOT NULL DEFAULT 0,
    block_today     INTEGER NOT NULL DEFAULT 0,
    spend_today_usd NUMERIC(10,4) NOT NULL DEFAULT 0,
    cpu_pct         REAL,
    ram_pct         REAL,
    browser_count   INTEGER,                          -- WORKERS=N concurrency (R5)
    sw_version      TEXT,                             -- reported version (R12)
    last_beat       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_heartbeat_stale ON worker_heartbeat (last_beat);

-- ---------------------------------------------------------------------------
-- poison_jobs: quarantine for jobs that crash/hang whoever claims them (R7).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS poison_jobs (
    url            TEXT PRIMARY KEY,
    crash_count    INTEGER NOT NULL DEFAULT 0,
    last_worker    TEXT,
    quarantined_at TIMESTAMPTZ,
    reason         TEXT,
    reviewed       BOOLEAN NOT NULL DEFAULT FALSE
);

-- ---------------------------------------------------------------------------
-- remote_commands: owner -> machine control (R7 remote-restart, R12 self-update).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS remote_commands (
    id             BIGSERIAL PRIMARY KEY,
    worker_id      TEXT,                              -- target worker_id ('*' = fleet-wide)
    command        TEXT NOT NULL,                     -- 'restart'|'pause'|'resume'|'self_update'|'drain'
    target_version TEXT,                              -- for 'self_update' (R12 canary)
    issued_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    acked_at       TIMESTAMPTZ                        -- hard global close (direct commands)
);
CREATE INDEX IF NOT EXISTS idx_commands_open ON remote_commands (worker_id) WHERE acked_at IS NULL;

-- command_acks: per-worker ack of a command, so a fleet-wide ('*') broadcast is
-- delivered to EVERY worker -- not consumed by whoever acks first (R7). poll_commands
-- excludes a command this worker has already acked here; a DIRECT command also closes
-- via remote_commands.acked_at. No FK (keeps the test TRUNCATE order-independent).
CREATE TABLE IF NOT EXISTS command_acks (
    command_id BIGINT      NOT NULL,
    worker_id  TEXT        NOT NULL,
    acked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (command_id, worker_id)
);
