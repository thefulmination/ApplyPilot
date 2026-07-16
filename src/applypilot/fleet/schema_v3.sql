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
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS liveness_required BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS liveness_status TEXT;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS liveness_reason TEXT;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS liveness_checked_at TIMESTAMPTZ;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS liveness_check_owner TEXT;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS liveness_check_expires_at TIMESTAMPTZ;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS liveness_check_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS liveness_consecutive_uncertain INTEGER NOT NULL DEFAULT 0;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS eligibility_required BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS eligibility_status TEXT;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS eligibility_reason TEXT;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS eligibility_checked_at TIMESTAMPTZ;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS infrastructure_failure_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS infrastructure_last_failure_at TIMESTAMPTZ;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS session_required BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS tenant_profile_id TEXT;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS routing_required BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS execution_route TEXT;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS host_policy TEXT;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS decision_id TEXT;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS policy_version TEXT;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS decision_action TEXT;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS qualification_verdict TEXT;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS qualification_score REAL;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS qualification_floor REAL;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS preference_score REAL;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS outcome_score REAL;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS final_score REAL;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS decision_confidence REAL;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS decision_created_at TIMESTAMPTZ;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS decision_expires_at TIMESTAMPTZ;
ALTER TABLE apply_queue ADD COLUMN IF NOT EXISTS input_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_apply_queue_dedup ON apply_queue (dedup_key);
CREATE INDEX IF NOT EXISTS idx_apply_queue_approved
    ON apply_queue (score DESC) WHERE status = 'queued' AND approved_batch IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_apply_queue_liveness
    ON apply_queue (liveness_checked_at, score DESC)
    WHERE status = 'queued' AND liveness_required;
CREATE INDEX IF NOT EXISTS idx_apply_queue_infrastructure_pending
    ON apply_queue (infrastructure_last_failure_at DESC)
    WHERE apply_status = 'infrastructure_pending';

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
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS last_window_roll_at    TIMESTAMPTZ;     -- nightly window roll guard
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS ats_apply_mode          TEXT NOT NULL DEFAULT 'stopped'; -- 'stopped'|'canary'|'steady'
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS canary_enabled         BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS canary_remaining        INTEGER;
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS linkedin_apply_mode     TEXT NOT NULL DEFAULT 'stopped'; -- 'stopped'|'canary'|'steady'
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS linkedin_canary_enabled  BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS linkedin_canary_remaining INTEGER;
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS daily_apply_target       INTEGER;        -- optional operator target; NULL = unconfigured
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS ats_policy_version TEXT;
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS linkedin_policy_version TEXT;
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS ats_policy_lane TEXT
    GENERATED ALWAYS AS ('ats'::text) STORED;
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS linkedin_policy_lane TEXT
    GENERATED ALWAYS AS ('linkedin'::text) STORED;

UPDATE fleet_config SET ats_apply_mode='stopped'
WHERE ats_apply_mode IS NULL OR ats_apply_mode NOT IN ('stopped', 'canary', 'steady');
UPDATE fleet_config SET linkedin_apply_mode='stopped'
WHERE linkedin_apply_mode IS NULL OR linkedin_apply_mode NOT IN ('stopped', 'canary', 'steady');

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
    url               TEXT NOT NULL,
    task              TEXT NOT NULL,                 -- 'score'|'audit'|'tailor'|'enrich'
    payload           JSONB,                         -- minimal context for the task
    status            fleet_task_status NOT NULL DEFAULT 'queued',
    lease_owner       TEXT,
    lease_expires_at  TIMESTAMPTZ,
    attempts          INTEGER NOT NULL DEFAULT 0,
    result            JSONB,                         -- advisory score/audit/tailored-resume ref
    est_cost_usd      NUMERIC(10,4) NOT NULL DEFAULT 0,
    synced_to_home_at TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (url, task)
);

DO $$
DECLARE
    pk_cols TEXT[];
BEGIN
    SELECT array_agg(a.attname ORDER BY u.ordinality)
      INTO pk_cols
    FROM pg_constraint c
    JOIN unnest(c.conkey) WITH ORDINALITY u(attnum, ordinality) ON true
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = u.attnum
    WHERE c.conrelid = 'compute_queue'::regclass
      AND c.contype = 'p';

    IF pk_cols = ARRAY['url'] THEN
        ALTER TABLE compute_queue DROP CONSTRAINT compute_queue_pkey;
        ALTER TABLE compute_queue ADD CONSTRAINT compute_queue_pkey PRIMARY KEY (url, task);
    ELSIF pk_cols IS NULL THEN
        ALTER TABLE compute_queue ADD CONSTRAINT compute_queue_pkey PRIMARY KEY (url, task);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_compute_lease ON compute_queue (status) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_compute_reclaim ON compute_queue (lease_expires_at) WHERE status = 'leased';
CREATE INDEX IF NOT EXISTS idx_compute_unsynced ON compute_queue (updated_at)
    WHERE status IN ('done','failed') AND synced_to_home_at IS NULL;

-- discovered_postings: raw JobSpy postings staged by lean discovery workers (no local brain).
-- The home box ingests these into the shared SQLite brain via store_jobspy_results (one write path).
CREATE TABLE IF NOT EXISTS discovered_postings (
    id                BIGSERIAL PRIMARY KEY,
    task_id           TEXT,
    source_label      TEXT,
    posting           JSONB NOT NULL,
    worker_id         TEXT,
    discovered_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    synced_to_home_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_discovered_unsynced ON discovered_postings (discovered_at)
    WHERE synced_to_home_at IS NULL;

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
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS worker_home_ip TEXT;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS target_host TEXT;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS lane TEXT NOT NULL DEFAULT 'linkedin';
ALTER TABLE linkedin_queue ALTER COLUMN lane SET DEFAULT 'linkedin';
UPDATE linkedin_queue SET lane='linkedin' WHERE lane IS DISTINCT FROM 'linkedin';
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS dedup_key TEXT;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS approved_batch TEXT;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS decision_id TEXT;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS policy_version TEXT;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS decision_action TEXT;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS qualification_verdict TEXT;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS qualification_score REAL;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS qualification_floor REAL;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS preference_score REAL;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS outcome_score REAL;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS final_score REAL;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS decision_confidence REAL;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS decision_created_at TIMESTAMPTZ;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS decision_expires_at TIMESTAMPTZ;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS input_hash TEXT;
-- apply-time channel recorder (zero LinkedIn scraping): how the apply actually happened.
--   apply_channel: 'easy_apply' (stayed on linkedin.com) | 'external' (redirected to an ATS)
--   apply_external_host: the ATS base host when external (e.g. 'ashbyhq.com', 'greenhouse.io')
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS apply_channel TEXT;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS apply_external_host TEXT;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS linkedin_resolve_status TEXT;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS linkedin_resolved_at TIMESTAMPTZ;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS linkedin_resolve_error TEXT;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS linkedin_unresolved_kind TEXT;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS linkedin_next_action TEXT;
-- A LinkedIn canary reservation is refundable only by the exact row/worker/attempt
-- that consumed it. These controller-owned markers are written only by the
-- SECURITY DEFINER transition functions at the end of this migration.
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS linkedin_canary_charge_attempt INTEGER;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS linkedin_canary_charge_worker TEXT;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS linkedin_canary_charge_policy_version TEXT;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS linkedin_canary_charge_capacity INTEGER;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS linkedin_canary_charge_exhausted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE linkedin_queue ADD COLUMN IF NOT EXISTS linkedin_canary_refunded_attempt INTEGER;

DO $$
DECLARE
    queue_name TEXT;
    target_oid OID;
    schema_oid OID := (SELECT oid FROM pg_namespace WHERE nspname = current_schema());
BEGIN
    FOREACH queue_name IN ARRAY ARRAY['apply_queue', 'linkedin_queue'] LOOP
        target_oid := to_regclass(format('%I.%I', current_schema(), queue_name));

        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', queue_name, queue_name || '_decision_action_ck');
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', queue_name, queue_name || '_qualification_verdict_ck');
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', queue_name, queue_name || '_confidence_ck');
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', queue_name, queue_name || '_expiry_ck');
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', queue_name, queue_name || '_decision_action_check');
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', queue_name, queue_name || '_qualification_verdict_check');
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', queue_name, queue_name || '_decision_confidence_check');

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = queue_name || '_canonical_provenance_ck'
              AND connamespace = schema_oid AND conrelid = target_oid
        ) THEN
            EXECUTE format($sql$
                ALTER TABLE %I ADD CONSTRAINT %I CHECK (
                    (
                        decision_id IS NULL AND policy_version IS NULL AND decision_action IS NULL
                        AND qualification_verdict IS NULL AND qualification_score IS NULL
                        AND qualification_floor IS NULL AND preference_score IS NULL
                        AND outcome_score IS NULL AND final_score IS NULL
                        AND decision_confidence IS NULL AND decision_created_at IS NULL
                        AND decision_expires_at IS NULL AND input_hash IS NULL
                    ) OR (
                        decision_id IS NOT NULL AND btrim(decision_id) <> ''
                        AND policy_version IS NOT NULL AND btrim(policy_version) <> ''
                        AND decision_action IN ('apply', 'review', 'reject')
                        AND qualification_verdict IN ('qualified', 'uncertain', 'unqualified')
                        AND qualification_score IS NOT NULL AND qualification_floor IS NOT NULL
                        AND preference_score IS NOT NULL AND outcome_score IS NOT NULL
                        AND final_score IS NOT NULL AND decision_confidence IS NOT NULL
                        AND qualification_score > '-Infinity'::REAL AND qualification_score < 'Infinity'::REAL
                        AND qualification_floor > '-Infinity'::REAL AND qualification_floor < 'Infinity'::REAL
                        AND preference_score > '-Infinity'::REAL AND preference_score < 'Infinity'::REAL
                        AND outcome_score > '-Infinity'::REAL AND outcome_score < 'Infinity'::REAL
                        AND final_score > '-Infinity'::REAL AND final_score < 'Infinity'::REAL
                        AND score = final_score
                        AND decision_confidence >= 0 AND decision_confidence <= 1
                        AND decision_created_at IS NOT NULL AND decision_expires_at IS NOT NULL
                        AND decision_expires_at > decision_created_at
                        AND input_hash IS NOT NULL AND btrim(input_hash) <> ''
                    )
                ) NOT VALID
            $sql$, queue_name, queue_name || '_canonical_provenance_ck');
            EXECUTE format('ALTER TABLE %I VALIDATE CONSTRAINT %I', queue_name, queue_name || '_canonical_provenance_ck');
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = queue_name || '_policy_lane_fk'
              AND connamespace = schema_oid AND conrelid = target_oid
        ) THEN
            EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (policy_version, lane) REFERENCES fleet_decision_policies(policy_version, lane) NOT VALID', queue_name, queue_name || '_policy_lane_fk');
            EXECUTE format('ALTER TABLE %I VALIDATE CONSTRAINT %I', queue_name, queue_name || '_policy_lane_fk');
        END IF;
    END LOOP;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'apply_queue_lane_ck' AND connamespace = schema_oid
          AND conrelid = to_regclass(format('%I.apply_queue', current_schema()))
    ) THEN
        ALTER TABLE apply_queue ADD CONSTRAINT apply_queue_lane_ck
            CHECK (policy_version IS NULL OR lane = 'ats') NOT VALID;
        ALTER TABLE apply_queue VALIDATE CONSTRAINT apply_queue_lane_ck;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'linkedin_queue_lane_ck' AND connamespace = schema_oid
          AND conrelid = to_regclass(format('%I.linkedin_queue', current_schema()))
    ) THEN
        ALTER TABLE linkedin_queue ADD CONSTRAINT linkedin_queue_lane_ck
            CHECK (policy_version IS NULL OR lane = 'linkedin') NOT VALID;
        ALTER TABLE linkedin_queue VALIDATE CONSTRAINT linkedin_queue_lane_ck;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fleet_config_ats_policy_fk' AND connamespace = schema_oid
          AND conrelid = to_regclass(format('%I.fleet_config', current_schema()))
    ) THEN
        ALTER TABLE fleet_config ADD CONSTRAINT fleet_config_ats_policy_fk
            FOREIGN KEY (ats_policy_version, ats_policy_lane)
            REFERENCES fleet_decision_policies(policy_version, lane) NOT VALID;
        ALTER TABLE fleet_config VALIDATE CONSTRAINT fleet_config_ats_policy_fk;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fleet_config_linkedin_policy_fk' AND connamespace = schema_oid
          AND conrelid = to_regclass(format('%I.fleet_config', current_schema()))
    ) THEN
        ALTER TABLE fleet_config ADD CONSTRAINT fleet_config_linkedin_policy_fk
            FOREIGN KEY (linkedin_policy_version, linkedin_policy_lane)
            REFERENCES fleet_decision_policies(policy_version, lane) NOT VALID;
        ALTER TABLE fleet_config VALIDATE CONSTRAINT fleet_config_linkedin_policy_fk;
    END IF;
END $$;

DO $$
DECLARE
    index_predicate TEXT;
BEGIN
    SELECT pg_get_expr(i.indpred, i.indrelid)
      INTO index_predicate
    FROM pg_index i
    JOIN pg_class idx ON idx.oid = i.indexrelid
    JOIN pg_class rel ON rel.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = idx.relnamespace AND n.oid = rel.relnamespace
    WHERE n.nspname = current_schema() AND rel.relname = 'apply_queue'
      AND idx.relname = 'idx_apply_queue_canonical_lease';
    IF index_predicate IS NOT NULL
       AND NOT (
           index_predicate LIKE '%policy_version IS NOT NULL%'
           AND index_predicate LIKE '%qualification_score IS NOT NULL%'
           AND index_predicate LIKE '%decision_created_at IS NOT NULL%'
           AND index_predicate LIKE '%decision_expires_at IS NOT NULL%'
           AND index_predicate LIKE '%input_hash IS NOT NULL%'
       ) THEN
        DROP INDEX idx_apply_queue_canonical_lease;
    END IF;

    SELECT pg_get_expr(i.indpred, i.indrelid)
      INTO index_predicate
    FROM pg_index i
    JOIN pg_class idx ON idx.oid = i.indexrelid
    JOIN pg_class rel ON rel.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = idx.relnamespace AND n.oid = rel.relnamespace
    WHERE n.nspname = current_schema() AND rel.relname = 'linkedin_queue'
      AND idx.relname = 'idx_linkedin_queue_canonical_lease';
    IF index_predicate IS NOT NULL
       AND NOT (
           index_predicate LIKE '%policy_version IS NOT NULL%'
           AND index_predicate LIKE '%qualification_score IS NOT NULL%'
           AND index_predicate LIKE '%decision_created_at IS NOT NULL%'
           AND index_predicate LIKE '%decision_expires_at IS NOT NULL%'
           AND index_predicate LIKE '%input_hash IS NOT NULL%'
       ) THEN
        DROP INDEX idx_linkedin_queue_canonical_lease;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_apply_queue_canonical_lease
    ON apply_queue (policy_version, decision_action, decision_expires_at, score DESC)
    WHERE status = 'queued' AND decision_id IS NOT NULL AND btrim(decision_id) <> ''
      AND policy_version IS NOT NULL AND btrim(policy_version) <> ''
      AND decision_action = 'apply' AND qualification_verdict = 'qualified'
      AND qualification_score IS NOT NULL AND qualification_floor IS NOT NULL
      AND preference_score IS NOT NULL AND outcome_score IS NOT NULL AND final_score IS NOT NULL
      AND decision_confidence IS NOT NULL AND decision_created_at IS NOT NULL
      AND decision_expires_at IS NOT NULL AND input_hash IS NOT NULL AND btrim(input_hash) <> '';
CREATE INDEX IF NOT EXISTS idx_linkedin_queue_canonical_lease
    ON linkedin_queue (policy_version, decision_action, decision_expires_at, score DESC)
    WHERE status = 'queued' AND decision_id IS NOT NULL AND btrim(decision_id) <> ''
      AND policy_version IS NOT NULL AND btrim(policy_version) <> ''
      AND decision_action = 'apply' AND qualification_verdict = 'qualified'
      AND qualification_score IS NOT NULL AND qualification_floor IS NOT NULL
      AND preference_score IS NOT NULL AND outcome_score IS NOT NULL AND final_score IS NOT NULL
      AND decision_confidence IS NOT NULL AND decision_created_at IS NOT NULL
      AND decision_expires_at IS NOT NULL AND input_hash IS NOT NULL AND btrim(input_hash) <> '';

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
ALTER TABLE rate_governor ADD COLUMN IF NOT EXISTS halted_until TIMESTAMPTZ;
-- A3: a recency stamp for EVERY lease/outcome (success + captcha + block), distinct from
-- last_applied_at (which is stamped ONLY on a confirmed apply). The ATS apply lease gates the
-- min-gap / Doctor floor off COALESCE(last_applied_at, last_attempt_at) so a never-succeeded
-- (hard-blocking) host -- whose last_applied_at is forever NULL -- is still spaced by the breaker
-- gap AND the Doctor's doctor_min_gap_floor, instead of leasing back-to-back at zero spacing.
ALTER TABLE rate_governor ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ;

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
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS provider TEXT;
-- provider indexed for the rolling per-agent apply-spend sum (agent_budget).
CREATE INDEX IF NOT EXISTS idx_llm_usage_provider_ts ON llm_usage (provider, ts);

-- ---------------------------------------------------------------------------
-- agent_availability: FLEET-WIDE apply-agent block state (one row per agent).
-- Two writers, one channel: (1) a worker that hits a usage/session wall records
-- blocked_until = the reset time so the WHOLE fleet skips that agent (not just the
-- one worker that discovered it -- the fleet-wide upgrade over per-worker memory);
-- (2) the predictive monitor (agent_budget.evaluate_soft_blocks) pre-emptively blocks
-- an agent whose rolling apply spend crosses its soft cap, BEFORE it walls. Workers
-- read this each tick and feed it into their AgentSwitcher. Layered ON TOP of the
-- per-worker reactive switch; never replaces it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_availability (
    agent         TEXT PRIMARY KEY,          -- 'claude' | 'codex' | 'deepseek'
    blocked_until TIMESTAMPTZ,               -- NULL or past = available now
    reason        TEXT,                      -- 'usage_limit_wall' | 'predictive_spend'
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
-- Phase 2.3: belt-and-suspenders for a live DB whose applied_set predates got_response
-- (CREATE TABLE IF NOT EXISTS above is a no-op on an already-existing table).
ALTER TABLE applied_set ADD COLUMN IF NOT EXISTS got_response BOOLEAN NOT NULL DEFAULT FALSE;

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
-- otp_request: Gmail relay bookkeeping (R4). The code is persisted only for the
-- short interval between the home responder answering and the worker consuming it.
-- This row is also the consumed audit trail; matched_message_id prevents one email
-- from being delivered to multiple requests, including across responder cycles.
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

-- Relay transport columns (2026-07-03): the CODE lives here only for the seconds
-- between the home responder answering and the worker consuming it, then is nulled.
ALTER TABLE otp_request ADD COLUMN IF NOT EXISTS code        TEXT;
ALTER TABLE otp_request ADD COLUMN IF NOT EXISTS code_kind   TEXT;   -- 'code' | 'magic_link'
ALTER TABLE otp_request ADD COLUMN IF NOT EXISTS expires_at  TIMESTAMPTZ;
ALTER TABLE otp_request ADD COLUMN IF NOT EXISTS answered_at TIMESTAMPTZ;
ALTER TABLE otp_request ADD COLUMN IF NOT EXISTS matched_message_id TEXT;
ALTER TABLE otp_request ADD COLUMN IF NOT EXISTS wait_started_at TIMESTAMPTZ;
-- The responder's pending-scan: unanswered, unconsumed requests.
CREATE INDEX IF NOT EXISTS idx_otp_pending ON otp_request (requested_at)
    WHERE code IS NULL AND consumed_at IS NULL;
-- DeadMan active-demand summary: skip expired/legacy history and cover all
-- timestamps needed by the count/oldest-wait query without visiting those rows.
CREATE INDEX IF NOT EXISTS idx_otp_active_wait
    ON otp_request (expires_at, requested_at, wait_started_at)
    WHERE code IS NULL AND consumed_at IS NULL
      AND wait_started_at IS NOT NULL AND expires_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_otp_matched_message_unique
    ON otp_request (matched_message_id) WHERE matched_message_id IS NOT NULL;

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
-- inbox_outcomes: thin per-EMAIL application-outcome summary pushed from the home
-- brain's email_events (SQLite outcomes tracker). One row per Gmail message_id
-- (idempotency anchor). Carries the R9 dedup_key so an outcome ties back to the
-- application cross-board. Read-only mirror; no body_text / PII crosses.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inbox_outcomes (
    message_id    TEXT PRIMARY KEY,
    dedup_key     TEXT,
    job_url       TEXT,
    company       TEXT,
    title         TEXT,
    stage         TEXT,
    outcome       TEXT,
    sender_domain TEXT,
    confidence    TEXT,
    occurred_at   TIMESTAMPTZ,
    pushed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_inbox_outcomes_dedup ON inbox_outcomes (dedup_key);
CREATE INDEX IF NOT EXISTS idx_inbox_outcomes_stage ON inbox_outcomes (stage, occurred_at);

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

-- fleet_desired_state is the single declarative actuator input for worker
-- enrollment.  Admission requires this table even while the fleet is paused;
-- desired_workers stays positive so a paused worker can heartbeat and prove
-- identity without receiving a lease.
CREATE TABLE IF NOT EXISTS fleet_desired_state (
    machine_owner TEXT PRIMARY KEY,
    desired_workers INTEGER NOT NULL DEFAULT 0 CHECK (desired_workers >= 0),
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    generation BIGINT NOT NULL DEFAULT 0,
    updated_by TEXT NOT NULL DEFAULT 'schema-bootstrap',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fleet_desired_state_freshness
    ON fleet_desired_state (updated_at);
ALTER TABLE fleet_desired_state
    ALTER COLUMN updated_by SET DEFAULT 'schema-bootstrap';

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
-- Crash + log visibility (shipped on every beat, OVERWRITE per beat). Both are
-- already SCRUBBED of secrets by the worker before the UPSERT; the console scrubs
-- again on read (defense in depth). Capped at write time (last_error <= 4000,
-- recent_log <= 8000) so this one-row-per-worker table cannot grow unbounded.
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS last_error  TEXT;
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS recent_log  TEXT;
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS current_agent TEXT;
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS current_model TEXT;
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS agent_chain TEXT;
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS last_agent_switch_at TIMESTAMPTZ;
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS last_agent_switch_reason TEXT;

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

-- ---------------------------------------------------------------------------
-- fleet_machine_blackout: central, expiring operator control for all fleet work
-- on selected machine labels. Launchers/agents read this before starting apply,
-- discovery, or compute work. Expired rows remain as audit history.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fleet_machine_blackout (
    id             BIGSERIAL PRIMARY KEY,
    name           TEXT NOT NULL,
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    starts_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    allow_patterns TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    block_patterns TEXT[] NOT NULL DEFAULT ARRAY['*']::TEXT[],
    reason         TEXT,
    created_by     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    cleared_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_fleet_machine_blackout_active
    ON fleet_machine_blackout (active, starts_at, expires_at);

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

-- ===========================================================================
-- FLEET DOCTOR v1 (this file's only auto-remediation layer).
--
-- The Doctor reads the centralized failure data (apply_queue.apply_error,
-- apply_status, target_host, worker_id) and applies BOUNDED, REVERSIBLE,
-- MONOTONICALLY-CONSERVATIVE auto-fixes. Per-worker recent_log/last_error are consumed
-- by the console and the Fleet Diagnoser (fleet/diagnoser.py), not the Doctor. Every auto action it takes can ONLY make
-- the fleet MORE conservative (skip a host, un-approve queued rows, quarantine a
-- poison url, pace down / pause, or RAISE a timeout within a ceiling); it can never
-- un-pause, re-approve, raise the spend cap, lower the gap, or touch LinkedIn. The
-- two tables below are its KNOBS (active conservative state) + its AUDIT LOG.
-- ===========================================================================

-- fleet_config: a NEW, bounded apply-agent timeout override. NULL = use the
-- env/default (APPLYPILOT_AGENT_TIMEOUT). The Doctor only ever RAISES it within a
-- ceiling (timeout_bump); a longer timeout is conservative (it lets a slow page
-- finish instead of being killed + retried, which would re-hit the host). The apply
-- worker prefers this value over the env when it is set.
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS agent_timeout_override INTEGER;

-- ===========================================================================
-- FLEET DOCTOR HARDENING (red-team H1-H19): EFFECT-level monotonicity.
-- ===========================================================================

-- H1 (CATASTROPHE FIX): an ATS-ONLY pause flag. The Doctor's lane-pause writes THIS,
-- never fleet_config.paused. The apply lease/worker honor ats_paused; the LinkedIn lane
-- (_LEASE_LINKEDIN + linkedin_should_halt) NEVER reads it, so a Doctor pause can never
-- halt the LinkedIn catastrophe lane. fleet_config.paused stays the shared operator/cost
-- kill switch (still read by BOTH lanes' should_halt); the Doctor is forbidden to touch it.
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS ats_paused BOOLEAN NOT NULL DEFAULT FALSE;

-- H2 (AGGREGATE BUDGET): per-day blast-radius counters the Doctor decrements against. The
-- date anchor rolls the counters once per UTC day. NULL anchor / mismatched day == fresh.
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS doctor_budget_day        DATE;
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS doctor_host_skips_today  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS doctor_pace_actions_today INTEGER NOT NULL DEFAULT 0;

-- H18 (above-the-fold signal): the Doctor's last-pass timestamp + active auto-fix snapshot,
-- so /api/status (the 4s poll) can show a "Doctor" card + fire a toast on a new auto-fix
-- WITHOUT folding the heavy diagnostics blob into the fast poll.
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS doctor_last_pass_at TIMESTAMPTZ;

-- H5/H8 (pause debounce + provenance): persist when the Doctor armed a lane-pause (2-pass
-- debounce) and remember that the ACTIVE ats_paused was set by the Doctor (so it auto-reverts
-- only its OWN pause and the console can label provenance). pause_source: NULL|'doctor'.
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS doctor_pause_armed_at TIMESTAMPTZ;
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS ats_pause_source      TEXT;

-- A6: consecutive-systemic-pass counter. The N4 systemic classifier emits ONE de-duplicated
-- distinct-severity alert only on the Nth consecutive systemic pass (so a single flagged home_IP
-- mis-classified as systemic doesn't spam, and a permanently-systemic fleet emits one escalation
-- rather than latching recommend-only silently). Reset to 0 on the first non-systemic pass.
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS doctor_systemic_streak INTEGER NOT NULL DEFAULT 0;

-- DeadMan monitor (Task 2): persisted alert state for the read-only fleet dead-man
-- detector (applypilot.fleet.deadman). deadman_alert is a '|'-joined "kind: detail"
-- summary of the currently-active alerts (NULL when healthy); deadman_alert_at is
-- when it was last set; deadman_hot_streak persists the running_hot consecutive-check
-- counter across invocations (deadman_check is pure and takes/returns it explicitly).
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS deadman_alert TEXT;
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS deadman_alert_at TIMESTAMPTZ;
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS deadman_hot_streak INTEGER NOT NULL DEFAULT 0;

-- N2 (DOCTOR-OWNED, NON-SHARED ACTUATORS): two columns the Doctor SOLELY owns on a host:<h>
-- governor scope, so the watchdog breaker (which owns min_gap_seconds/base_min_gap_seconds/
-- breaker_state) and the Doctor never clobber each other.
--   doctor_min_gap_floor  -- H4: the lease uses GREATEST(min_gap_seconds, doctor_min_gap_floor)
--                            so a Doctor pace is monotone-by-construction and the breaker's
--                            min_gap restore can never wipe it.
--   doctor_skip_until     -- H6: host_skip becomes a self-expiring leasable FILTER (the lease
--                            adds AND COALESCE(doctor_skip_until,'-infinity') < now()) instead
--                            of NULLing approved_batch -- vetted approval is preserved.
ALTER TABLE rate_governor ADD COLUMN IF NOT EXISTS doctor_min_gap_floor INTEGER;
ALTER TABLE rate_governor ADD COLUMN IF NOT EXISTS doctor_skip_until    TIMESTAMPTZ;
-- NOTE: the fleet_diagnoses audit columns (H19/H13) are added AFTER that table is created,
-- at the bottom of this file -- ADD COLUMN here would fail (the table doesn't exist yet).

-- fleet_knobs: ACTIVE conservative state the Doctor sets (and a human can REVERSE).
-- A host_skip knob makes a re-push/approve respect the skip; a timeout_bump knob is
-- the audit twin of fleet_config.agent_timeout_override. TTL'd via expires_at so a
-- transient host block self-heals; the sweep deactivates expired rows each run.
CREATE TABLE IF NOT EXISTS fleet_knobs (
    id          BIGSERIAL PRIMARY KEY,
    knob_type   TEXT NOT NULL,                      -- 'host_skip'|'timeout_bump'|'quarantine'|'pace_or_pause'
    scope_key   TEXT,                               -- host / lane / url the knob applies to
    value_text  TEXT,                               -- e.g. the new timeout, the new min_gap, 'paused'
    reason      TEXT,
    created_by  TEXT NOT NULL DEFAULT 'doctor',
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_fleet_knobs_active
    ON fleet_knobs (active, knob_type, scope_key);

-- H9 (singleton / race-proof idempotency): at most ONE active knob per (knob_type, scope_key).
-- With INSERT ... ON CONFLICT DO NOTHING this makes a duplicate-knob write from two racing
-- Doctor passes a no-op at the DB level (the advisory lock in run_doctor is the first line of
-- defense; this index is the backstop). A12: the de-dup of a NULL scope_key works ONLY because
-- of COALESCE(scope_key,''): Postgres treats two NULLs as DISTINCT in a UNIQUE index (NULLs do
-- NOT collide), so WITHOUT the COALESCE two active (knob_type, NULL) knobs would both be allowed
-- and the H9 race would re-open for any NULL-scope knob. The COALESCE(...,'') is therefore
-- LOAD-BEARING -- it maps NULL -> '' so they collide. Do NOT "simplify" it away.
CREATE UNIQUE INDEX IF NOT EXISTS uq_fleet_knobs_one_active
    ON fleet_knobs (knob_type, COALESCE(scope_key, '')) WHERE active;

-- fleet_diagnoses: the Doctor's AUDIT LOG + the human RECOMMENDATION queue. Every
-- auto action writes one row (what + why + evidence + how_to_reverse + expires_at);
-- everything the Doctor finds that is NOT one of the four conservative auto-fixes
-- becomes a status='recommended' row for a human (never auto-applied).
CREATE TABLE IF NOT EXISTS fleet_diagnoses (
    id             BIGSERIAL PRIMARY KEY,
    cluster_key    TEXT,                             -- reason|host|machine|lane signature (idempotency key)
    reason         TEXT,
    host           TEXT,
    machine        TEXT,
    lane           TEXT,
    sample_count   INT,
    severity       TEXT,                             -- 'info'|'warn'|'severe'
    diagnosis      TEXT,                             -- rule-templated, NO LLM in v1 (hook reserved)
    recommendation TEXT,
    auto_action    TEXT,                             -- the knob_type applied, or NULL for a recommendation
    how_to_reverse TEXT,
    status         TEXT NOT NULL DEFAULT 'open',     -- open|auto_applied|recommended|applied|dismissed|reverted|expired
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_fleet_diagnoses_status
    ON fleet_diagnoses (status, created_at);
CREATE INDEX IF NOT EXISTS idx_fleet_diagnoses_cluster
    ON fleet_diagnoses (cluster_key) WHERE status IN ('auto_applied','recommended','open');

-- fleet_console_audit: append-only operator console action trail. This table records
-- existing allow-listed console actions only; it never stores request bodies, tokens,
-- DSNs, prompts, browser logs, resume/profile data, or raw secrets.
CREATE TABLE IF NOT EXISTS fleet_console_audit (
    id         BIGSERIAL PRIMARY KEY,
    action     TEXT NOT NULL,
    actor      TEXT,
    lane       TEXT,
    target     TEXT,
    message    TEXT,
    ok         BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fleet_console_audit_created
    ON fleet_console_audit (created_at DESC);

-- autotriage_actions: audit trail for autonomous bounded LLM/rule triage.
-- The LLM may choose only from a fixed action menu, and the executor validates
-- each action before mutating fleet state. This table records both applied and
-- rejected choices plus manual-review deferrals so the loop is inspectable and reversible.
CREATE TABLE IF NOT EXISTS autotriage_actions (
    id                 BIGSERIAL PRIMARY KEY,
    url                TEXT,
    worker_id          TEXT,
    chosen_action      TEXT,
    decision_source    TEXT,
    confidence         REAL,
    reason             TEXT,
    action_status      TEXT NOT NULL DEFAULT 'planned',
    prior_status       TEXT,
    prior_attempts     INTEGER,
    prior_apply_error  TEXT,
    evidence           JSONB,
    how_to_reverse     TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_autotriage_url
    ON autotriage_actions (url, created_at);
CREATE INDEX IF NOT EXISTS idx_autotriage_status
    ON autotriage_actions (action_status, created_at);

-- apply_attempts: durable adapter submit boundary. An unresolved irreversible
-- action for a dedup key excludes every second submit path until it is verified,
-- contradicted, or quarantined by the owner-controlled workflow.
CREATE TABLE IF NOT EXISTS apply_attempts (
    attempt_id          UUID PRIMARY KEY,
    queue_name          TEXT NOT NULL,
    url                 TEXT NOT NULL,
    dedup_key           TEXT,
    worker_id           TEXT NOT NULL,
    route               TEXT NOT NULL,
    route_version       TEXT,
    state               TEXT NOT NULL CHECK (state IN (
        'prepared', 'submit_started', 'submitted_unverified', 'verified',
        'contradicted', 'quarantined', 'failed_pre_submit'
    )),
    submit_started_at   TIMESTAMPTZ,
    finalized_at        TIMESTAMPTZ,
    verification_method TEXT,
    verification_ref   TEXT,
    evidence            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_apply_attempts_unresolved_dedup
    ON apply_attempts (dedup_key)
    WHERE dedup_key IS NOT NULL
      AND state IN ('submit_started', 'submitted_unverified');
CREATE INDEX IF NOT EXISTS idx_apply_attempts_url_created
    ON apply_attempts (queue_name, url, created_at DESC);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fleet_worker') THEN
        -- Role reconciliation owns the exact column grants. A schema rerun must
        -- never restore the former table-wide INSERT/UPDATE privileges.
        REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON apply_attempts FROM fleet_worker;
    END IF;
END $$;

-- apply_result_events: durable per-job terminal evidence written by the worker when
-- it closes a lease. worker_heartbeat.recent_log is only a moving tail; repair tools
-- should prefer this table for job-specific result evidence.
CREATE TABLE IF NOT EXISTS apply_result_events (
    id                  BIGSERIAL PRIMARY KEY,
    queue_name          TEXT NOT NULL DEFAULT 'apply_queue',
    url                 TEXT NOT NULL,
    worker_id           TEXT,
    machine_owner       TEXT,
    status              TEXT,
    apply_status        TEXT,
    apply_error         TEXT,
    target_host         TEXT,
    home_ip             TEXT,
    agent               TEXT,
    agent_model         TEXT,
    est_cost_usd        REAL,
    apply_duration_ms   INTEGER,
    application_tool_calls INTEGER,
    job_log_path        TEXT,
    transcript_digest   TEXT,
    final_result_source TEXT,
    result_metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_line         TEXT,
    source              TEXT NOT NULL DEFAULT 'worker',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS queue_name TEXT NOT NULL DEFAULT 'apply_queue';
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS machine_owner TEXT;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'worker';
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS route TEXT;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS failure_class TEXT;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS tool_calls_total INTEGER;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS application_tool_calls INTEGER;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS last_tool TEXT;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS host_policy TEXT;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS result_metadata JSONB;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS job_log_path TEXT;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS transcript_digest TEXT;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS final_result_source TEXT;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS evidence_is_assertion BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS result_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
UPDATE apply_result_events SET result_metadata = '{}'::jsonb WHERE result_metadata IS NULL;
ALTER TABLE apply_result_events ALTER COLUMN result_metadata SET DEFAULT '{}'::jsonb;
ALTER TABLE apply_result_events ALTER COLUMN result_metadata SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_apply_result_events_url_created
    ON apply_result_events (queue_name, url, created_at DESC);

-- The worker role has no direct queue or authority-ledger privileges.  Every
-- worker transition is represented by this opaque, immutable lease identity.
ALTER TABLE public.apply_queue ADD COLUMN IF NOT EXISTS worker_lease_id UUID;
ALTER TABLE public.linkedin_queue ADD COLUMN IF NOT EXISTS worker_lease_id UUID;
ALTER TABLE public.fleet_config ADD COLUMN IF NOT EXISTS linkedin_owner_ip TEXT;

CREATE TABLE IF NOT EXISTS public.fleet_worker_lease_ledger (
    lease_id UUID PRIMARY KEY,
    lane TEXT NOT NULL CHECK (lane IN ('ats', 'linkedin')),
    url TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    queue_attempt INTEGER NOT NULL,
    policy_version TEXT NOT NULL,
    home_ip TEXT,
    target_host TEXT,
    canary_charged BOOLEAN NOT NULL DEFAULT FALSE,
    canary_capacity_before INTEGER,
    canary_exhausted BOOLEAN NOT NULL DEFAULT FALSE,
    refunded_at TIMESTAMPTZ,
    browser_interaction_at TIMESTAMPTZ,
    state TEXT NOT NULL DEFAULT 'leased' CHECK (
        state IN ('leased', 'parked', 'requeued', 'terminal')
    ),
    leased_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.now(),
    closed_at TIMESTAMPTZ,
    UNIQUE (lane, url, queue_attempt, worker_id, policy_version)
);
CREATE INDEX IF NOT EXISTS idx_fleet_worker_lease_active
    ON public.fleet_worker_lease_ledger (lane, url, worker_id)
    WHERE state IN ('leased', 'parked');

-- Controller-synchronized authority data.  Workers can neither read nor alter
-- it; lease functions enforce it internally.
CREATE TABLE IF NOT EXISTS public.fleet_worker_blocklist (
    kind TEXT NOT NULL CHECK (kind IN ('company', 'pattern')),
    value TEXT NOT NULL,
    PRIMARY KEY (kind, value)
);

CREATE TABLE IF NOT EXISTS public.fleet_worker_principals (
    role_name NAME PRIMARY KEY,
    worker_id TEXT NOT NULL REFERENCES public.workers(worker_id) ON DELETE CASCADE,
    contract TEXT NOT NULL CHECK (contract IN ('apply','linkedin','compute','discovery')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_fleet_worker_principals_worker_contract
    ON public.fleet_worker_principals(worker_id, contract);

CREATE OR REPLACE FUNCTION public.fleet_worker_mark_browser_interaction()
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $fleet_worker_mark_browser_interaction$
DECLARE mapped_worker TEXT; active_lease UUID;
BEGIN
    SELECT p.worker_id INTO mapped_worker FROM public.fleet_worker_principals p
    WHERE p.role_name=session_user AND p.contract IN ('apply','linkedin');
    active_lease:=NULLIF(pg_catalog.current_setting('applypilot.worker_lease_id',TRUE),'')::uuid;
    IF mapped_worker IS NULL OR active_lease IS NULL THEN RETURN FALSE; END IF;
    UPDATE public.fleet_worker_lease_ledger SET browser_interaction_at=COALESCE(browser_interaction_at,pg_catalog.now())
    WHERE lease_id=active_lease AND worker_id=mapped_worker AND state='leased';
    RETURN FOUND;
END
$fleet_worker_mark_browser_interaction$;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_mark_browser_interaction() FROM PUBLIC;

-- Remote workers can update queue-owned lease columns directly, but cannot safely
-- mutate controller-owned canary controls. These functions are the only worker
-- transition boundary for decrement/refund. Every object reference is qualified;
-- the fixed search_path prevents object-shadowing attacks under SECURITY DEFINER.
CREATE OR REPLACE FUNCTION public.fleet_worker_authorize_lease(
    p_lane TEXT,
    p_url TEXT,
    p_worker TEXT,
    p_attempt INTEGER
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $fleet_worker_authorize_lease$
DECLARE
    cfg public.fleet_config%ROWTYPE;
    ats_row public.apply_queue%ROWTYPE;
    linkedin_row public.linkedin_queue%ROWTYPE;
    capacity_before INTEGER;
    capacity_after INTEGER;
BEGIN
    IF p_url IS NULL OR p_worker IS NULL OR p_attempt IS NULL OR p_attempt <= 0 THEN
        RAISE EXCEPTION 'invalid fleet worker lease identity'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT * INTO STRICT cfg FROM public.fleet_config WHERE id = 1 FOR UPDATE;

    IF p_lane = 'ats' THEN
        SELECT * INTO ats_row
        FROM public.apply_queue
        WHERE url = p_url
          AND lane = 'ats'
          AND status = 'leased'
          AND lease_owner = p_worker
          AND attempts = p_attempt
        FOR UPDATE;

        IF NOT FOUND
           OR COALESCE(cfg.paused, FALSE)
           OR COALESCE(cfg.ats_paused, FALSE)
           OR ats_row.approved_batch IS NULL
           OR ats_row.decision_id IS NULL
           OR ats_row.policy_version IS DISTINCT FROM cfg.ats_policy_version
           OR ats_row.decision_action IS DISTINCT FROM 'apply'
           OR ats_row.qualification_verdict IS DISTINCT FROM 'qualified'
           OR ats_row.qualification_score < ats_row.qualification_floor
           OR ats_row.decision_expires_at <= pg_catalog.now()
           OR ats_row.score IS DISTINCT FROM ats_row.final_score
           OR NOT EXISTS (
                SELECT 1 FROM public.fleet_decision_policies p
                WHERE p.policy_version = ats_row.policy_version
                  AND p.lane = 'ats'
                  AND p.status IN ('canary', 'active')
           ) THEN
            RAISE EXCEPTION 'ATS lease gates changed before canary authorization'
                USING ERRCODE = 'check_violation';
        END IF;

        IF cfg.ats_apply_mode = 'steady' THEN
            RETURN TRUE;
        END IF;
        IF cfg.ats_apply_mode IS DISTINCT FROM 'canary'
           OR NOT COALESCE(cfg.canary_enabled, FALSE)
           OR COALESCE(cfg.canary_remaining, 0) <= 0 THEN
            RAISE EXCEPTION 'ATS canary capacity changed before lease authorization'
                USING ERRCODE = 'check_violation';
        END IF;

        capacity_after := cfg.canary_remaining - 1;
        UPDATE public.fleet_config
        SET canary_remaining = capacity_after,
            ats_apply_mode = CASE WHEN capacity_after = 0 THEN 'stopped' ELSE ats_apply_mode END
        WHERE id = 1;
        RETURN TRUE;
    ELSIF p_lane = 'linkedin' THEN
        SELECT * INTO linkedin_row
        FROM public.linkedin_queue
        WHERE url = p_url
          AND lane = 'linkedin'
          AND status = 'leased'
          AND lease_owner = p_worker
          AND attempts = p_attempt
        FOR UPDATE;

        IF NOT FOUND
           OR COALESCE(cfg.paused, FALSE)
           OR linkedin_row.approved_batch IS NULL
           OR linkedin_row.decision_id IS NULL
           OR linkedin_row.policy_version IS DISTINCT FROM cfg.linkedin_policy_version
           OR linkedin_row.decision_action IS DISTINCT FROM 'apply'
           OR linkedin_row.qualification_verdict IS DISTINCT FROM 'qualified'
           OR linkedin_row.qualification_score < linkedin_row.qualification_floor
           OR linkedin_row.decision_expires_at <= pg_catalog.now()
           OR linkedin_row.score IS DISTINCT FROM linkedin_row.final_score
           OR NOT EXISTS (
                SELECT 1 FROM public.fleet_decision_policies p
                WHERE p.policy_version = linkedin_row.policy_version
                  AND p.lane = 'linkedin'
                  AND p.status IN ('canary', 'active')
           ) THEN
            RAISE EXCEPTION 'LinkedIn lease gates changed before canary authorization'
                USING ERRCODE = 'check_violation';
        END IF;

        IF cfg.linkedin_apply_mode = 'steady' THEN
            UPDATE public.linkedin_queue
            SET linkedin_canary_charge_attempt = NULL,
                linkedin_canary_charge_worker = NULL,
                linkedin_canary_charge_policy_version = NULL,
                linkedin_canary_charge_capacity = NULL,
                linkedin_canary_charge_exhausted = FALSE
            WHERE url = p_url;
            RETURN TRUE;
        END IF;
        IF cfg.linkedin_apply_mode IS DISTINCT FROM 'canary'
           OR NOT COALESCE(cfg.linkedin_canary_enabled, FALSE)
           OR COALESCE(cfg.linkedin_canary_remaining, 0) <= 0 THEN
            RAISE EXCEPTION 'LinkedIn canary capacity changed before lease authorization'
                USING ERRCODE = 'check_violation';
        END IF;

        capacity_before := cfg.linkedin_canary_remaining;
        capacity_after := capacity_before - 1;
        UPDATE public.fleet_config
        SET linkedin_canary_remaining = capacity_after,
            linkedin_apply_mode = CASE
                WHEN capacity_after = 0 THEN 'stopped'
                ELSE linkedin_apply_mode
            END
        WHERE id = 1;
        UPDATE public.linkedin_queue
        SET linkedin_canary_charge_attempt = p_attempt,
            linkedin_canary_charge_worker = p_worker,
            linkedin_canary_charge_policy_version = linkedin_row.policy_version,
            linkedin_canary_charge_capacity = capacity_before,
            linkedin_canary_charge_exhausted = (capacity_after = 0),
            linkedin_canary_refunded_attempt = NULL
        WHERE url = p_url;
        RETURN TRUE;
    END IF;

    RAISE EXCEPTION 'unsupported fleet worker lease lane: %', p_lane
        USING ERRCODE = 'check_violation';
END
$fleet_worker_authorize_lease$;

CREATE OR REPLACE FUNCTION public.fleet_worker_refund_linkedin_canary(
    p_url TEXT,
    p_worker TEXT,
    p_attempt INTEGER
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $fleet_worker_refund_linkedin_canary$
DECLARE
    cfg public.fleet_config%ROWTYPE;
    queue_row public.linkedin_queue%ROWTYPE;
    refunded_capacity INTEGER;
    may_reopen BOOLEAN;
BEGIN
    SELECT * INTO STRICT cfg FROM public.fleet_config WHERE id = 1 FOR UPDATE;
    SELECT * INTO queue_row
    FROM public.linkedin_queue
    WHERE url = p_url
      AND status = 'leased'
      AND lease_owner = p_worker
      AND attempts = p_attempt
    FOR UPDATE;

    IF NOT FOUND
       OR queue_row.linkedin_canary_charge_attempt IS DISTINCT FROM p_attempt
       OR queue_row.linkedin_canary_charge_worker IS DISTINCT FROM p_worker
       OR queue_row.linkedin_canary_charge_capacity IS NULL
       OR queue_row.linkedin_canary_refunded_attempt IS NOT DISTINCT FROM p_attempt THEN
        RETURN FALSE;
    END IF;

    refunded_capacity := LEAST(
        COALESCE(cfg.linkedin_canary_remaining, 0) + 1,
        queue_row.linkedin_canary_charge_capacity
    );
    may_reopen := queue_row.linkedin_canary_charge_exhausted
        AND cfg.linkedin_apply_mode = 'stopped'
        AND COALESCE(cfg.linkedin_canary_remaining, 0) = 0
        AND refunded_capacity > 0
        AND NOT COALESCE(cfg.paused, FALSE)
        AND COALESCE(cfg.linkedin_canary_enabled, FALSE)
        AND cfg.linkedin_policy_version IS NOT DISTINCT FROM
            queue_row.linkedin_canary_charge_policy_version
        AND queue_row.policy_version IS NOT DISTINCT FROM
            queue_row.linkedin_canary_charge_policy_version
        AND EXISTS (
            SELECT 1 FROM public.fleet_decision_policies p
            WHERE p.policy_version = queue_row.linkedin_canary_charge_policy_version
              AND p.lane = 'linkedin'
              AND p.status IN ('canary', 'active')
        );

    UPDATE public.fleet_config
    SET linkedin_canary_remaining = refunded_capacity,
        linkedin_apply_mode = CASE WHEN may_reopen THEN 'canary' ELSE linkedin_apply_mode END
    WHERE id = 1;
    UPDATE public.linkedin_queue
    SET linkedin_canary_refunded_attempt = p_attempt
    WHERE url = p_url;
    RETURN TRUE;
END
$fleet_worker_refund_linkedin_canary$;

REVOKE ALL PRIVILEGES ON FUNCTION
    public.fleet_worker_authorize_lease(TEXT, TEXT, TEXT, INTEGER)
    FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION
    public.fleet_worker_refund_linkedin_canary(TEXT, TEXT, INTEGER)
    FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.fleet_worker_lease_ats(
    p_worker TEXT,
    p_home_ip TEXT,
    p_ttl INTEGER,
    p_sw_version TEXT,
    p_liveness_fresh INTEGER
) RETURNS SETOF public.apply_queue
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $fleet_worker_lease_ats$
DECLARE
    cfg public.fleet_config%ROWTYPE;
    job public.apply_queue%ROWTYPE;
    leased public.apply_queue%ROWTYPE;
    new_lease UUID := pg_catalog.gen_random_uuid();
    expected_version TEXT;
    actual_version TEXT;
    capacity_before INTEGER;
    principal_contract TEXT;
    enrolled public.workers%ROWTYPE;
    mapped_worker TEXT;
    desired_ok BOOLEAN := TRUE;
    reserved INTEGER;
BEGIN
    SELECT * INTO STRICT cfg FROM public.fleet_config WHERE id=1 FOR UPDATE;
    SELECT p.worker_id,p.contract INTO mapped_worker,principal_contract
    FROM public.fleet_worker_principals p WHERE p.role_name=session_user;
    IF FOUND THEN
        p_worker:=mapped_worker;
        IF principal_contract<>'apply' THEN
            RAISE EXCEPTION 'worker principal is not authorized for ATS' USING ERRCODE='insufficient_privilege';
        END IF;
        SELECT * INTO STRICT enrolled FROM public.workers w WHERE w.worker_id=p_worker FOR UPDATE;
        IF NOT enrolled.validated OR enrolled.revoked_at IS NOT NULL OR enrolled.public_ip IS NULL THEN RETURN; END IF;
        IF pg_catalog.to_regclass('public.fleet_desired_state') IS NULL THEN RETURN; END IF;
        EXECUTE 'SELECT EXISTS(SELECT 1 FROM public.fleet_desired_state d '
          'WHERE d.machine_owner=$1 AND d.desired_workers>0 '
          'AND d.updated_at>=pg_catalog.now()-interval ''5 minutes'')'
          INTO desired_ok USING enrolled.machine_owner;
        IF NOT desired_ok THEN RETURN; END IF;
        p_home_ip:=enrolled.public_ip;
        SELECT h.sw_version INTO p_sw_version FROM public.worker_heartbeat h
        WHERE h.worker_id=p_worker AND h.last_beat>=pg_catalog.now()-interval '120 seconds';
        IF p_sw_version IS NULL THEN RETURN; END IF;
    ELSIF session_user <> current_user THEN
        RAISE EXCEPTION 'unmapped worker principal' USING ERRCODE='insufficient_privilege';
    END IF;
    p_ttl:=LEAST(GREATEST(COALESCE(p_ttl,1200),60),1200);
    p_liveness_fresh:=900;
    IF p_worker IS NULL OR pg_catalog.btrim(p_worker) = '' OR p_ttl <= 0 THEN
        RAISE EXCEPTION 'invalid ATS worker lease request' USING ERRCODE='check_violation';
    END IF;
    expected_version := CASE
        WHEN cfg.canary_worker_id = p_worker AND cfg.canary_version IS NOT NULL
            THEN cfg.canary_version
        ELSE cfg.pinned_worker_version
    END;
    SELECT h.sw_version INTO actual_version FROM public.worker_heartbeat h
    WHERE h.worker_id=p_worker;
    actual_version:=COALESCE(p_sw_version,actual_version);
    IF expected_version IS NOT NULL AND actual_version IS DISTINCT FROM expected_version
       AND (session_user='fleet_worker' OR actual_version IS NOT NULL) THEN
        RETURN;
    END IF;
    IF COALESCE(cfg.paused,FALSE) OR COALESCE(cfg.ats_paused,FALSE)
       OR cfg.ats_apply_mode NOT IN ('canary','steady')
       OR (cfg.ats_apply_mode='canary' AND (
            NOT COALESCE(cfg.canary_enabled,FALSE) OR COALESCE(cfg.canary_remaining,0) <= 0
       )) THEN
        RETURN;
    END IF;
    PERFORM 1 FROM public.rate_governor g
    WHERE g.scope_key IN ('global','home_ip:'||p_home_ip)
    ORDER BY g.scope_key FOR UPDATE;
    GET DIAGNOSTICS reserved=ROW_COUNT;
    IF reserved<>2 THEN
      RAISE EXCEPTION 'controller must configure global and worker home governor scopes'
        USING ERRCODE='check_violation';
    END IF;

    SELECT q.* INTO job
    FROM public.apply_queue q
    JOIN public.rate_governor host
      ON host.scope_key='host:' || COALESCE(q.target_host,q.apply_domain)
    JOIN public.rate_governor home ON home.scope_key='home_ip:' || p_home_ip
    JOIN public.rate_governor glob ON glob.scope_key='global'
    WHERE q.status='queued' AND q.lane='ats' AND q.approved_batch IS NOT NULL
      AND q.decision_id IS NOT NULL AND q.policy_version=cfg.ats_policy_version
      AND q.decision_action='apply' AND q.qualification_verdict='qualified'
      AND q.qualification_score >= q.qualification_floor
      AND q.decision_expires_at > pg_catalog.now() AND q.score=q.final_score
      AND (cfg.approval_threshold IS NULL OR q.final_score >= cfg.approval_threshold)
      AND COALESCE(q.apply_error,'') NOT ILIKE 'requeued_by_%'
      AND NOT EXISTS (
          SELECT 1 FROM public.apply_result_events prior
          WHERE prior.queue_name='apply_queue' AND prior.url=q.url
            AND (COALESCE(prior.application_tool_calls,0)>0
                 OR COALESCE(prior.apply_error,'') ILIKE 'requeued_by_%')
      )
      AND EXISTS (
          SELECT 1 FROM public.fleet_decision_policies p
          WHERE p.policy_version=q.policy_version AND p.lane='ats'
            AND p.status IN ('canary','active')
      )
      AND NOT EXISTS (
          SELECT 1 FROM public.apply_attempts a
          WHERE a.dedup_key=q.dedup_key
            AND a.state IN ('submit_started','submitted_unverified')
      )
      AND NOT EXISTS (SELECT 1 FROM public.applied_set a WHERE a.dedup_key=q.dedup_key)
      AND NOT EXISTS (
          SELECT 1 FROM public.fleet_worker_blocklist b
          WHERE (b.kind='company' AND pg_catalog.lower(pg_catalog.btrim(COALESCE(q.company,'')))=b.value)
             OR (b.kind='pattern' AND (q.url ILIKE b.value OR COALESCE(q.application_url,'') ILIKE b.value))
      )
      AND (COALESCE(cfg.spend_cap_usd,0) <= 0 OR
           (SELECT COALESCE(pg_catalog.sum(x.cumulative_cost_usd),0) FROM public.apply_queue x)
               < cfg.spend_cap_usd)
      AND glob.scope_key IS NOT NULL AND glob.count_24h < glob.daily_cap
      AND COALESCE(glob.breaker_state,'ok') NOT IN ('paused','demoted')
      AND COALESCE(home.breaker_state,'ok') <> 'demoted'
      AND NOT (COALESCE(home.breaker_state,'ok')='paused'
               AND COALESCE(home.breaker_until,'infinity'::timestamptz)>=pg_catalog.now())
      AND home.scope_key IS NOT NULL AND home.count_24h < home.daily_cap
      AND host.scope_key IS NOT NULL AND COALESCE(host.breaker_state,'ok') <> 'demoted'
      AND NOT (COALESCE(host.breaker_state,'ok')='paused'
               AND COALESCE(host.breaker_until,'infinity'::timestamptz)>=pg_catalog.now())
      AND COALESCE(host.count_24h,0) < COALESCE(host.daily_cap,2147483647)
      AND COALESCE(host.doctor_skip_until,'-infinity'::timestamptz)<pg_catalog.now()
      AND (COALESCE(host.last_applied_at,host.last_attempt_at) IS NULL OR
           COALESCE(host.last_applied_at,host.last_attempt_at) < pg_catalog.now()
             - pg_catalog.make_interval(secs=>GREATEST(
                  COALESCE(host.min_gap_seconds,90),COALESCE(host.doctor_min_gap_floor,0))))
      AND (NOT COALESCE(q.liveness_required,FALSE) OR (
          q.liveness_status='live' AND q.liveness_checked_at >= pg_catalog.now()
            - pg_catalog.make_interval(secs=>p_liveness_fresh)))
      AND (NOT COALESCE(q.eligibility_required,FALSE) OR q.eligibility_status='eligible')
      AND (NOT COALESCE(q.routing_required,FALSE) OR q.execution_route='deterministic')
    ORDER BY q.score DESC,q.url
    LIMIT 1 FOR UPDATE OF q,host SKIP LOCKED;
    IF NOT FOUND THEN RETURN; END IF;

    capacity_before := cfg.canary_remaining;
    IF cfg.ats_apply_mode='canary' THEN
        UPDATE public.fleet_config
        SET canary_remaining=canary_remaining-1,
            ats_apply_mode=CASE WHEN canary_remaining-1=0 THEN 'stopped' ELSE ats_apply_mode END
        WHERE id=1 AND ats_apply_mode='canary' AND canary_enabled
          AND canary_remaining>0 AND ats_policy_version=job.policy_version;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'ATS lease gates changed during authorization'
                USING ERRCODE='check_violation';
        END IF;
    END IF;
    UPDATE public.rate_governor SET count_24h=count_24h+1,
      last_attempt_at=pg_catalog.now(),updated_at=pg_catalog.now()
    WHERE scope_key IN ('global','home_ip:'||p_home_ip,
      'host:'||COALESCE(job.target_host,job.apply_domain)) AND count_24h<daily_cap;
    GET DIAGNOSTICS reserved=ROW_COUNT;
    IF reserved<>3 THEN
      RAISE EXCEPTION 'ATS governor capacity changed during authorization'
        USING ERRCODE='check_violation';
    END IF;
    UPDATE public.apply_queue q SET
        status='leased', lease_owner=p_worker,
        lease_expires_at=pg_catalog.now()+pg_catalog.make_interval(secs=>p_ttl),
        last_attempted_at=pg_catalog.now(), attempts=q.attempts+1,
        updated_at=pg_catalog.now(), worker_home_ip=p_home_ip, worker_lease_id=new_lease
    WHERE q.url=job.url AND q.status='queued'
    RETURNING q.* INTO STRICT leased;
    INSERT INTO public.fleet_worker_lease_ledger(
        lease_id,lane,url,worker_id,queue_attempt,policy_version,home_ip,target_host,
        canary_charged,canary_capacity_before,canary_exhausted
    ) VALUES (
        new_lease,'ats',leased.url,p_worker,
        (SELECT COALESCE(MAX(l.queue_attempt),0)+1 FROM public.fleet_worker_lease_ledger l
         WHERE l.lane='ats' AND l.url=leased.url),
        leased.policy_version,p_home_ip,
        COALESCE(leased.target_host,leased.apply_domain),cfg.ats_apply_mode='canary',capacity_before,
        cfg.ats_apply_mode='canary' AND capacity_before=1
    );
    INSERT INTO public.apply_result_events(
        queue_name,url,worker_id,status,apply_status,target_host,result_metadata,result_line,source
    ) VALUES ('apply_queue',leased.url,p_worker,'leased','leased',
        COALESCE(leased.target_host,leased.apply_domain),
        '{"worker_assertion":{"execution_evidence":"lease_started"}}'::jsonb,
        'RESULT:lease_started','worker_transition');
    PERFORM pg_catalog.set_config('applypilot.worker_lease_id',new_lease::text,FALSE);
    RETURN NEXT leased;
END
$fleet_worker_lease_ats$;

DROP FUNCTION IF EXISTS public.fleet_worker_lease_linkedin(TEXT,TEXT,TEXT,INTEGER);
CREATE OR REPLACE FUNCTION public.fleet_worker_lease_linkedin(
    p_worker TEXT,
    p_public_ip TEXT,
    p_owner_ip TEXT,
    p_ttl INTEGER,
    p_sw_version TEXT
) RETURNS SETOF public.linkedin_queue
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $fleet_worker_lease_linkedin$
DECLARE
    cfg public.fleet_config%ROWTYPE;
    job public.linkedin_queue%ROWTYPE;
    leased public.linkedin_queue%ROWTYPE;
    new_lease UUID := pg_catalog.gen_random_uuid();
    capacity_before INTEGER;
    expected_version TEXT;
    actual_version TEXT;
    principal_contract TEXT;
    enrolled public.workers%ROWTYPE;
    mapped_worker TEXT;
    desired_ok BOOLEAN := TRUE;
    reserved INTEGER;
BEGIN
    SELECT * INTO STRICT cfg FROM public.fleet_config WHERE id=1 FOR UPDATE;
    SELECT p.worker_id,p.contract INTO mapped_worker,principal_contract
    FROM public.fleet_worker_principals p WHERE p.role_name=session_user;
    IF FOUND THEN
        p_worker:=mapped_worker;
        IF principal_contract<>'linkedin' THEN
            RAISE EXCEPTION 'worker principal is not authorized for LinkedIn' USING ERRCODE='insufficient_privilege';
        END IF;
        SELECT * INTO STRICT enrolled FROM public.workers w WHERE w.worker_id=p_worker FOR UPDATE;
        IF NOT enrolled.validated OR enrolled.revoked_at IS NOT NULL OR enrolled.public_ip IS NULL THEN RETURN; END IF;
        IF pg_catalog.to_regclass('public.fleet_desired_state') IS NULL THEN RETURN; END IF;
        EXECUTE 'SELECT EXISTS(SELECT 1 FROM public.fleet_desired_state d '
          'WHERE d.machine_owner=$1 AND d.desired_workers>0 '
          'AND d.updated_at>=pg_catalog.now()-interval ''5 minutes'')'
          INTO desired_ok USING enrolled.machine_owner;
        IF NOT desired_ok THEN RETURN; END IF;
        p_public_ip:=enrolled.public_ip;
        p_owner_ip:=cfg.linkedin_owner_ip;
        SELECT h.sw_version INTO p_sw_version FROM public.worker_heartbeat h
        WHERE h.worker_id=p_worker AND h.last_beat>=pg_catalog.now()-interval '120 seconds';
        IF p_sw_version IS NULL THEN RETURN; END IF;
    ELSIF session_user <> current_user THEN
        RAISE EXCEPTION 'unmapped worker principal' USING ERRCODE='insufficient_privilege';
    END IF;
    p_ttl:=LEAST(GREATEST(COALESCE(p_ttl,1200),60),1200);
    IF p_worker IS NULL OR p_public_ip IS NULL OR p_owner_ip IS NULL
       OR p_public_ip<>p_owner_ip OR p_ttl<=0 THEN RETURN; END IF;
    expected_version := CASE
        WHEN cfg.canary_worker_id=p_worker AND cfg.canary_version IS NOT NULL THEN cfg.canary_version
        ELSE cfg.pinned_worker_version
    END;
    SELECT h.sw_version INTO actual_version FROM public.worker_heartbeat h WHERE h.worker_id=p_worker;
    actual_version:=COALESCE(p_sw_version,actual_version);
    IF expected_version IS NOT NULL AND actual_version IS DISTINCT FROM expected_version
       AND (session_user='fleet_worker' OR actual_version IS NOT NULL) THEN RETURN; END IF;
    IF cfg.linkedin_owner_ip IS NULL OR cfg.linkedin_owner_ip<>p_public_ip THEN RETURN; END IF;
    IF COALESCE(cfg.paused,FALSE) OR cfg.linkedin_apply_mode NOT IN ('canary','steady')
       OR (cfg.linkedin_apply_mode='canary' AND (
           NOT COALESCE(cfg.linkedin_canary_enabled,FALSE)
           OR COALESCE(cfg.linkedin_canary_remaining,0)<=0)) THEN RETURN; END IF;
    PERFORM 1 FROM public.rate_governor g
    WHERE g.scope_key IN ('account:linkedin','global') ORDER BY g.scope_key FOR UPDATE;
    GET DIAGNOSTICS reserved=ROW_COUNT;
    IF reserved<>2 THEN
        RAISE EXCEPTION 'controller must configure account:linkedin and global governor scopes'
            USING ERRCODE='check_violation';
    END IF;
    SELECT q.* INTO job
    FROM public.linkedin_queue q
    JOIN public.rate_governor a ON a.scope_key='account:linkedin'
    JOIN public.rate_governor g ON g.scope_key='global'
    WHERE q.status='queued' AND q.lane='linkedin' AND q.approved_batch IS NOT NULL
      AND q.score>=GREATEST(COALESCE(cfg.approval_threshold,7),7)
      AND q.linkedin_resolve_status IN ('easy_apply','resolved_offsite')
      AND q.linkedin_resolved_at>=pg_catalog.now()-pg_catalog.make_interval(days=>3)
      AND q.decision_id IS NOT NULL AND q.policy_version=cfg.linkedin_policy_version
      AND q.decision_action='apply' AND q.qualification_verdict='qualified'
      AND q.qualification_score>=q.qualification_floor
      AND q.decision_expires_at>pg_catalog.now() AND q.score=q.final_score
      AND EXISTS (SELECT 1 FROM public.fleet_decision_policies p
          WHERE p.policy_version=q.policy_version AND p.lane='linkedin'
            AND p.status IN ('canary','active'))
      AND (a.halted_until IS NULL OR a.halted_until<pg_catalog.now())
      AND a.count_24h<a.daily_cap AND COALESCE(a.breaker_state,'ok')<>'demoted'
      AND NOT (COALESCE(a.breaker_state,'ok')='paused'
               AND COALESCE(a.breaker_until,'infinity'::timestamptz)>=pg_catalog.now())
      AND (a.last_applied_at IS NULL OR a.last_applied_at<pg_catalog.now()
           -pg_catalog.make_interval(secs=>COALESCE(a.min_gap_seconds,1200)))
      AND g.count_24h<g.daily_cap AND COALESCE(g.breaker_state,'ok') NOT IN ('paused','demoted')
      AND NOT EXISTS (SELECT 1 FROM public.applied_set d WHERE d.dedup_key=q.dedup_key)
      AND NOT EXISTS (SELECT 1 FROM public.fleet_worker_blocklist b
          WHERE (b.kind='company' AND pg_catalog.lower(pg_catalog.btrim(COALESCE(q.company,'')))=b.value)
             OR (b.kind='pattern' AND (q.url ILIKE b.value OR COALESCE(q.application_url,'') ILIKE b.value)))
    ORDER BY q.score DESC,q.url LIMIT 1 FOR UPDATE OF q SKIP LOCKED;
    IF NOT FOUND THEN RETURN; END IF;
    capacity_before:=cfg.linkedin_canary_remaining;
    IF cfg.linkedin_apply_mode='canary' THEN
        UPDATE public.fleet_config SET
          linkedin_canary_remaining=linkedin_canary_remaining-1,
          linkedin_apply_mode=CASE WHEN linkedin_canary_remaining-1=0 THEN 'stopped'
                                   ELSE linkedin_apply_mode END
        WHERE id=1 AND linkedin_apply_mode='canary' AND linkedin_canary_enabled
          AND linkedin_canary_remaining>0 AND linkedin_policy_version=job.policy_version;
        IF NOT FOUND THEN RAISE EXCEPTION 'LinkedIn lease gates changed during authorization'
            USING ERRCODE='check_violation'; END IF;
    END IF;
    UPDATE public.rate_governor SET count_24h=count_24h+1,
        last_applied_at=pg_catalog.now(),updated_at=pg_catalog.now()
    WHERE scope_key IN ('account:linkedin','global') AND count_24h<daily_cap;
    GET DIAGNOSTICS reserved=ROW_COUNT;
    IF reserved<>2 THEN
      RAISE EXCEPTION 'LinkedIn governor capacity changed during authorization'
        USING ERRCODE='check_violation';
    END IF;
    UPDATE public.linkedin_queue q SET status='leased',lease_owner=p_worker,
        lease_expires_at=pg_catalog.now()+pg_catalog.make_interval(secs=>p_ttl),
        last_attempted_at=pg_catalog.now(),attempts=q.attempts+1,updated_at=pg_catalog.now(),
        worker_home_ip=p_public_ip,worker_lease_id=new_lease
    WHERE q.url=job.url AND q.status='queued' RETURNING q.* INTO STRICT leased;
    INSERT INTO public.fleet_worker_lease_ledger(
        lease_id,lane,url,worker_id,queue_attempt,policy_version,home_ip,target_host,
        canary_charged,canary_capacity_before,canary_exhausted
    ) VALUES (new_lease,'linkedin',leased.url,p_worker,
        (SELECT COALESCE(MAX(l.queue_attempt),0)+1 FROM public.fleet_worker_lease_ledger l
         WHERE l.lane='linkedin' AND l.url=leased.url),
        leased.policy_version,
        p_public_ip,COALESCE(leased.target_host,'linkedin.com'),cfg.linkedin_apply_mode='canary',
        capacity_before,cfg.linkedin_apply_mode='canary' AND capacity_before=1);
    PERFORM pg_catalog.set_config('applypilot.worker_lease_id',new_lease::text,FALSE);
    RETURN NEXT leased;
END
$fleet_worker_lease_linkedin$;

CREATE OR REPLACE FUNCTION public.fleet_worker_requeue(
    p_lane TEXT,p_url TEXT,p_worker TEXT,p_error TEXT
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $fleet_worker_requeue$
DECLARE
    lease public.fleet_worker_lease_ledger%ROWTYPE;
    cfg public.fleet_config%ROWTYPE;
    drift BOOLEAN;
    refunded INTEGER;
    mapped_worker TEXT;
BEGIN
    SELECT p.worker_id INTO mapped_worker FROM public.fleet_worker_principals p
    WHERE p.role_name=session_user
      AND p.contract=CASE WHEN p_lane='ats' THEN 'apply' ELSE p_lane END;
    IF FOUND THEN p_worker:=mapped_worker;
    ELSIF session_user<>current_user THEN
      RAISE EXCEPTION 'unmapped or cross-contract worker principal' USING ERRCODE='insufficient_privilege';
    END IF;
    SELECT * INTO lease FROM public.fleet_worker_lease_ledger l
    WHERE l.lane=p_lane AND l.url=p_url AND l.worker_id=p_worker AND l.state='leased'
    ORDER BY l.leased_at DESC LIMIT 1 FOR UPDATE;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    SELECT * INTO STRICT cfg FROM public.fleet_config WHERE id=1 FOR UPDATE;
    drift := (CASE WHEN p_lane='ats' THEN cfg.ats_policy_version
                   ELSE cfg.linkedin_policy_version END) IS DISTINCT FROM lease.policy_version
      OR NOT EXISTS (SELECT 1 FROM public.fleet_decision_policies p
          WHERE p.policy_version=lease.policy_version AND p.lane=p_lane
            AND p.status IN ('canary','active'));
    IF drift THEN
      IF p_lane='ats' THEN UPDATE public.fleet_config SET ats_apply_mode='stopped' WHERE id=1;
      ELSE UPDATE public.fleet_config SET linkedin_apply_mode='stopped' WHERE id=1; END IF;
    END IF;
    IF lease.browser_interaction_at IS NOT NULL THEN
      IF p_lane='ats' THEN
        UPDATE public.apply_queue SET status='crash_unconfirmed',apply_status='verification_pending',
          apply_error='requeue_denied_after_interaction',lease_owner=NULL,lease_expires_at=NULL,updated_at=pg_catalog.now()
        WHERE url=p_url AND worker_lease_id=lease.lease_id;
      ELSE
        UPDATE public.linkedin_queue SET status='crash_unconfirmed',apply_status='verification_pending',
          apply_error='requeue_denied_after_interaction',lease_owner=NULL,lease_expires_at=NULL,updated_at=pg_catalog.now()
        WHERE url=p_url AND worker_lease_id=lease.lease_id;
      END IF;
      UPDATE public.fleet_worker_lease_ledger SET state='parked',closed_at=pg_catalog.now()
      WHERE lease_id=lease.lease_id;
      RETURN FALSE;
    END IF;
    IF session_user<>current_user AND (
         NULLIF(pg_catalog.current_setting('applypilot.worker_lease_id',TRUE),'') IS NULL
         OR NULLIF(pg_catalog.current_setting('applypilot.worker_lease_id',TRUE),'')::uuid
            IS DISTINCT FROM lease.lease_id
       )
    THEN RETURN FALSE; END IF;
    IF p_lane='ats' THEN
        PERFORM 1 FROM public.apply_queue q WHERE q.url=p_url AND q.status='leased'
          AND q.lease_owner=p_worker AND q.worker_lease_id=lease.lease_id FOR UPDATE;
    ELSIF p_lane='linkedin' THEN
        PERFORM 1 FROM public.linkedin_queue q WHERE q.url=p_url AND q.status='leased'
          AND q.lease_owner=p_worker AND q.worker_lease_id=lease.lease_id FOR UPDATE;
    ELSE RAISE EXCEPTION 'unsupported worker lane' USING ERRCODE='check_violation';
    END IF;
    IF NOT FOUND THEN RETURN FALSE; END IF;

    IF lease.canary_charged AND lease.refunded_at IS NULL THEN
        IF p_lane='ats' THEN
            refunded:=LEAST(COALESCE(cfg.canary_remaining,0)+1,lease.canary_capacity_before);
            UPDATE public.fleet_config SET canary_remaining=refunded,
              ats_apply_mode=CASE WHEN drift THEN 'stopped'
                WHEN lease.canary_exhausted AND ats_apply_mode='stopped' AND NOT paused
                  AND NOT ats_paused AND canary_enabled THEN 'canary' ELSE ats_apply_mode END
            WHERE id=1;
        ELSE
            refunded:=LEAST(COALESCE(cfg.linkedin_canary_remaining,0)+1,lease.canary_capacity_before);
            UPDATE public.fleet_config SET linkedin_canary_remaining=refunded,
              linkedin_apply_mode=CASE WHEN drift THEN 'stopped'
                WHEN lease.canary_exhausted AND linkedin_apply_mode='stopped' AND NOT paused
                  AND linkedin_canary_enabled THEN 'canary' ELSE linkedin_apply_mode END
            WHERE id=1;
        END IF;
        UPDATE public.fleet_worker_lease_ledger SET refunded_at=pg_catalog.now()
        WHERE lease_id=lease.lease_id AND refunded_at IS NULL;
    END IF;
    IF p_lane='ats' THEN
        UPDATE public.apply_queue SET status='queued',apply_error=p_error,
          attempts=GREATEST(attempts-1,0),lease_owner=NULL,lease_expires_at=NULL,
          worker_lease_id=NULL,updated_at=pg_catalog.now() WHERE url=p_url;
        UPDATE public.rate_governor SET count_24h=GREATEST(count_24h-1,0),
          updated_at=pg_catalog.now()
        WHERE scope_key IN ('global','home_ip:'||lease.home_ip,'host:'||lease.target_host);
    ELSE
        UPDATE public.linkedin_queue SET status='queued',apply_status=NULL,apply_error=p_error,
          attempts=GREATEST(attempts-1,0),lease_owner=NULL,lease_expires_at=NULL,
          worker_lease_id=NULL,updated_at=pg_catalog.now() WHERE url=p_url;
        UPDATE public.rate_governor SET
          last_applied_at=CASE WHEN count_24h<=1 THEN NULL ELSE last_applied_at END,
          count_24h=GREATEST(count_24h-1,0),updated_at=pg_catalog.now()
        WHERE scope_key IN ('account:linkedin','global');
    END IF;
    UPDATE public.fleet_worker_lease_ledger SET state='requeued',closed_at=pg_catalog.now()
    WHERE lease_id=lease.lease_id;
    PERFORM pg_catalog.set_config('applypilot.worker_lease_id','',FALSE);
    RETURN TRUE;
END
$fleet_worker_requeue$;

CREATE OR REPLACE FUNCTION public.fleet_worker_park_infrastructure(
    p_url TEXT,p_worker TEXT,p_error TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $fleet_worker_park_infrastructure$
DECLARE
    lease public.fleet_worker_lease_ledger%ROWTYPE;
    cfg public.fleet_config%ROWTYPE;
    mapped_worker TEXT;
    refunded INTEGER;
    failure_count INTEGER;
BEGIN
    SELECT p.worker_id INTO mapped_worker FROM public.fleet_worker_principals p
    WHERE p.role_name=session_user AND p.contract='apply';
    IF FOUND THEN p_worker:=mapped_worker;
    ELSIF session_user<>current_user THEN
      RAISE EXCEPTION 'unmapped or cross-contract worker principal' USING ERRCODE='insufficient_privilege';
    END IF;
    SELECT * INTO lease FROM public.fleet_worker_lease_ledger l
    WHERE l.lane='ats' AND l.url=p_url AND l.worker_id=p_worker AND l.state='leased'
    ORDER BY l.leased_at DESC LIMIT 1 FOR UPDATE;
    IF NOT FOUND OR lease.browser_interaction_at IS NOT NULL THEN RETURN NULL; END IF;
    IF NULLIF(pg_catalog.current_setting('applypilot.worker_lease_id',TRUE),'') IS NULL
       OR NULLIF(pg_catalog.current_setting('applypilot.worker_lease_id',TRUE),'')::uuid
          IS DISTINCT FROM lease.lease_id
    THEN RETURN NULL; END IF;
    PERFORM 1 FROM public.apply_queue q WHERE q.url=p_url AND q.status='leased'
      AND q.lease_owner=p_worker AND q.worker_lease_id=lease.lease_id FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT * INTO STRICT cfg FROM public.fleet_config WHERE id=1 FOR UPDATE;
    IF lease.canary_charged AND lease.refunded_at IS NULL THEN
      refunded:=LEAST(COALESCE(cfg.canary_remaining,0)+1,lease.canary_capacity_before);
      UPDATE public.fleet_config SET canary_remaining=refunded,
        ats_apply_mode=CASE
          WHEN lease.canary_exhausted AND ats_apply_mode='stopped' AND NOT paused
            AND NOT ats_paused AND canary_enabled THEN 'canary'
          ELSE ats_apply_mode END
      WHERE id=1;
      UPDATE public.fleet_worker_lease_ledger SET refunded_at=pg_catalog.now()
      WHERE lease_id=lease.lease_id AND refunded_at IS NULL;
    END IF;
    UPDATE public.apply_queue SET status='failed',apply_status='infrastructure_pending',
      apply_error=pg_catalog.left(COALESCE(NULLIF(p_error,''),'browser_preflight'),200),
      attempts=GREATEST(attempts-1,0),
      infrastructure_failure_count=COALESCE(infrastructure_failure_count,0)+1,
      infrastructure_last_failure_at=pg_catalog.now(),lease_owner=NULL,lease_expires_at=NULL,
      worker_lease_id=NULL,updated_at=pg_catalog.now()
    WHERE url=p_url AND worker_lease_id=lease.lease_id
    RETURNING infrastructure_failure_count INTO failure_count;
    IF NOT FOUND THEN RETURN NULL; END IF;
    UPDATE public.rate_governor SET count_24h=GREATEST(count_24h-1,0),
      updated_at=pg_catalog.now()
    WHERE scope_key IN ('global','home_ip:'||lease.home_ip,'host:'||lease.target_host);
    UPDATE public.fleet_worker_lease_ledger SET state='terminal',closed_at=pg_catalog.now()
    WHERE lease_id=lease.lease_id;
    PERFORM pg_catalog.set_config('applypilot.worker_lease_id','',FALSE);
    RETURN pg_catalog.jsonb_build_object(
      'status','failed','apply_status','infrastructure_pending',
      'infrastructure_failure_count',failure_count);
END
$fleet_worker_park_infrastructure$;

CREATE OR REPLACE FUNCTION public.fleet_worker_terminalize(
    p_lane TEXT,p_url TEXT,p_worker TEXT,p_status TEXT,p_apply_status TEXT,
    p_apply_error TEXT,p_evidence JSONB
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $fleet_worker_terminalize$
DECLARE
    lease public.fleet_worker_lease_ledger%ROWTYPE;
    aq public.apply_queue%ROWTYPE;
    lq public.linkedin_queue%ROWTYPE;
    qname TEXT;
    company_value TEXT;
    application_value TEXT;
    dedup_value TEXT;
    outcome_col TEXT;
    cost_value NUMERIC := COALESCE((p_evidence->>'est_cost_usd')::numeric,0);
    mapped_worker TEXT;
    queue_status TEXT := CASE WHEN p_status='applied' THEN 'crash_unconfirmed' ELSE p_status END;
    recorded_status TEXT := CASE WHEN p_status='applied' THEN 'submission_claimed' ELSE p_status END;
BEGIN
    SELECT p.worker_id INTO mapped_worker FROM public.fleet_worker_principals p
    WHERE p.role_name=session_user
      AND p.contract=CASE WHEN p_lane='ats' THEN 'apply' ELSE p_lane END;
    IF FOUND THEN p_worker:=mapped_worker;
    ELSIF session_user<>current_user THEN
      RAISE EXCEPTION 'unmapped or cross-contract worker principal' USING ERRCODE='insufficient_privilege';
    END IF;
    IF p_status NOT IN ('applied','failed','blocked','captcha','crash_unconfirmed') THEN
        RAISE EXCEPTION 'unsupported worker terminal status' USING ERRCODE='check_violation';
    END IF;
    IF pg_catalog.octet_length(COALESCE(p_evidence,'{}'::jsonb)::text)>16384
       OR cost_value<0 OR cost_value>100
    THEN RAISE EXCEPTION 'worker terminal evidence is invalid or oversized'
      USING ERRCODE='program_limit_exceeded'; END IF;
    SELECT * INTO lease FROM public.fleet_worker_lease_ledger l
    WHERE l.lane=p_lane AND l.url=p_url AND l.worker_id=p_worker
      AND l.state IN ('leased','parked') ORDER BY l.leased_at DESC LIMIT 1 FOR UPDATE;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    IF session_user<>current_user AND (
         NULLIF(pg_catalog.current_setting('applypilot.worker_lease_id',TRUE),'') IS NULL
         OR NULLIF(pg_catalog.current_setting('applypilot.worker_lease_id',TRUE),'')::uuid
            IS DISTINCT FROM lease.lease_id
       )
    THEN RETURN FALSE; END IF;
    IF p_lane='ats' THEN
        UPDATE public.apply_queue q SET status=queue_status::public.apply_queue_status,
          apply_status=CASE WHEN p_status='applied' THEN 'submission_claimed_unverified' ELSE p_apply_status END,
          apply_error=p_apply_error,est_cost_usd=cost_value,
          cumulative_cost_usd=COALESCE(q.cumulative_cost_usd,0)+cost_value,
          agent_model=p_evidence->>'agent_model',
          apply_duration_ms=(p_evidence->>'apply_duration_ms')::integer,
          applied_at=q.applied_at,
          worker_id=p_worker,lease_owner=NULL,lease_expires_at=NULL,updated_at=pg_catalog.now()
        WHERE q.url=p_url AND q.lease_owner=p_worker AND q.worker_lease_id=lease.lease_id
        RETURNING q.* INTO aq;
        IF NOT FOUND THEN RETURN FALSE; END IF;
        qname:='apply_queue'; company_value:=aq.company; application_value:=aq.application_url;
        dedup_value:=aq.dedup_key;
    ELSIF p_lane='linkedin' THEN
        UPDATE public.linkedin_queue q SET status=queue_status::public.apply_queue_status,
          apply_status=CASE WHEN p_status='applied' THEN 'submission_claimed_unverified' ELSE p_apply_status END,
          apply_error=p_apply_error,est_cost_usd=cost_value,
          agent_model=p_evidence->>'agent_model',
          apply_duration_ms=(p_evidence->>'apply_duration_ms')::integer,
          apply_channel=COALESCE(p_evidence->>'apply_channel',q.apply_channel),
          apply_external_host=COALESCE(p_evidence->>'apply_external_host',q.apply_external_host),
          applied_at=q.applied_at,
          worker_id=p_worker,lease_owner=NULL,lease_expires_at=NULL,updated_at=pg_catalog.now()
        WHERE q.url=p_url AND q.lease_owner=p_worker AND q.worker_lease_id=lease.lease_id
        RETURNING q.* INTO lq;
        IF NOT FOUND THEN RETURN FALSE; END IF;
        qname:='linkedin_queue'; company_value:=lq.company; application_value:=lq.application_url;
        dedup_value:=lq.dedup_key;
    ELSE RAISE EXCEPTION 'unsupported worker lane' USING ERRCODE='check_violation';
    END IF;
    UPDATE public.auth_challenge c SET resolved_at=pg_catalog.now(),
      outcome='superseded:'||p_status
    WHERE c.url=p_url AND c.resolved_at IS NULL
      AND NOT EXISTS (
        SELECT 1 FROM public.apply_queue a
        WHERE p_lane='linkedin' AND a.url=p_url AND a.apply_status='challenge_pending'
        UNION ALL
        SELECT 1 FROM public.linkedin_queue l
        WHERE p_lane='ats' AND l.url=p_url AND l.apply_status='challenge_pending'
      );
    outcome_col:=CASE p_status
        WHEN 'blocked' THEN 'block_24h' WHEN 'captcha' THEN 'captcha_24h' END;
    IF p_status='applied' THEN
      IF p_lane='linkedin' THEN
        UPDATE public.rate_governor SET last_attempt_at=pg_catalog.now(),updated_at=pg_catalog.now()
        WHERE scope_key='account:linkedin';
      ELSE
        UPDATE public.rate_governor SET last_attempt_at=pg_catalog.now(),updated_at=pg_catalog.now()
        WHERE scope_key IN ('global','host:'||lease.target_host,'home_ip:'||lease.home_ip);
      END IF;
      IF NOT FOUND THEN RAISE EXCEPTION 'required worker governor disappeared'
        USING ERRCODE='object_not_in_prerequisite_state'; END IF;
    END IF;
    IF outcome_col IS NOT NULL THEN
        IF p_lane='linkedin' THEN
            UPDATE public.rate_governor SET
              success_24h=success_24h+(outcome_col='success_24h')::integer,
              block_24h=block_24h+(outcome_col='block_24h')::integer,
              captcha_24h=captcha_24h+(outcome_col='captcha_24h')::integer,
              updated_at=pg_catalog.now() WHERE scope_key='account:linkedin';
        ELSE
            UPDATE public.rate_governor SET
              success_24h=success_24h+(outcome_col='success_24h')::integer,
              block_24h=block_24h+(outcome_col='block_24h')::integer,
              captcha_24h=captcha_24h+(outcome_col='captcha_24h')::integer,
              count_24h=count_24h,
              last_attempt_at=pg_catalog.now(),
              last_applied_at=last_applied_at,
              updated_at=pg_catalog.now()
            WHERE scope_key IN ('global','host:'||lease.target_host,'home_ip:'||lease.home_ip);
        END IF;
    END IF;
    INSERT INTO public.apply_result_events(
      queue_name,url,worker_id,machine_owner,status,apply_status,apply_error,target_host,home_ip,
      agent,agent_model,est_cost_usd,apply_duration_ms,application_tool_calls,job_log_path,
      transcript_digest,final_result_source,result_metadata,result_line,source,route,failure_class,
      tool_calls_total,last_tool,host_policy,evidence_is_assertion
    ) VALUES(qname,p_url,p_worker,p_evidence->>'machine_owner',recorded_status,
      CASE WHEN p_status='applied' THEN 'submission_claimed_unverified' ELSE p_apply_status END,p_apply_error,
      lease.target_host,lease.home_ip,p_evidence->>'agent',p_evidence->>'agent_model',cost_value,
      (p_evidence->>'apply_duration_ms')::integer,(p_evidence->>'application_tool_calls')::integer,
      p_evidence->>'job_log_path',p_evidence->>'transcript_digest',p_evidence->>'final_result_source',
      COALESCE(p_evidence->'result_metadata','{}'::jsonb),
      CASE WHEN p_status='applied' THEN 'RESULT:SUBMISSION_CLAIMED_UNVERIFIED'
           ELSE 'RESULT:'||COALESCE(p_apply_error,p_apply_status,p_status) END,
      'worker',p_evidence->>'route',p_evidence->>'failure_class',
      (p_evidence->>'tool_calls_total')::integer,p_evidence->>'last_tool',p_evidence->>'host_policy',TRUE);
    IF cost_value>0 THEN
      INSERT INTO public.llm_usage(worker_id,machine_owner,task,model,provider,cost_usd)
      VALUES(p_worker,p_evidence->>'machine_owner','apply_agent',p_evidence->>'agent_model',
             p_evidence->>'agent',cost_value);
    END IF;
    UPDATE public.fleet_worker_lease_ledger SET state='terminal',closed_at=pg_catalog.now()
    WHERE lease_id=lease.lease_id;
    IF p_status='applied' THEN
      UPDATE public.apply_attempts SET state='submitted_unverified'
      WHERE queue_name=qname AND url=p_url AND worker_id=p_worker AND state='submit_started';
    END IF;
    PERFORM pg_catalog.set_config('applypilot.worker_lease_id','',FALSE);
    RETURN TRUE;
END
$fleet_worker_terminalize$;

CREATE OR REPLACE FUNCTION public.fleet_worker_park(
    p_lane TEXT,p_url TEXT,p_worker TEXT,p_kind TEXT,p_halt_seconds INTEGER,p_evidence JSONB
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $fleet_worker_park$
DECLARE lease public.fleet_worker_lease_ledger%ROWTYPE; mapped_worker TEXT;
BEGIN
    SELECT p.worker_id INTO mapped_worker FROM public.fleet_worker_principals p
    WHERE p.role_name=session_user
      AND p.contract=CASE WHEN p_lane='ats' THEN 'apply' ELSE p_lane END;
    IF FOUND THEN p_worker:=mapped_worker;
    ELSIF session_user<>current_user THEN
      RAISE EXCEPTION 'unmapped or cross-contract worker principal' USING ERRCODE='insufficient_privilege';
    END IF;
    IF pg_catalog.octet_length(COALESCE(p_evidence,'{}'::jsonb)::text)>8192 THEN
      RAISE EXCEPTION 'challenge evidence exceeds limit' USING ERRCODE='program_limit_exceeded'; END IF;
    SELECT * INTO lease FROM public.fleet_worker_lease_ledger l
    WHERE l.lane=p_lane AND l.url=p_url AND l.worker_id=p_worker AND l.state='leased'
    ORDER BY l.leased_at DESC LIMIT 1 FOR UPDATE;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    IF NULLIF(pg_catalog.current_setting('applypilot.worker_lease_id',TRUE),'') IS NULL
       OR NULLIF(pg_catalog.current_setting('applypilot.worker_lease_id',TRUE),'')::uuid IS DISTINCT FROM lease.lease_id
    THEN RETURN FALSE; END IF;
    IF p_lane='ats' THEN
      UPDATE public.apply_queue SET apply_status='challenge_pending',
        est_cost_usd=LEAST(GREATEST(COALESCE((p_evidence->>'est_cost_usd')::numeric,0),0),100),
        cumulative_cost_usd=COALESCE(cumulative_cost_usd,0)+LEAST(GREATEST(COALESCE((p_evidence->>'est_cost_usd')::numeric,0),0),100),
        agent_model=pg_catalog.left(p_evidence->>'agent_model',200),
        apply_duration_ms=(p_evidence->>'apply_duration_ms')::integer,worker_id=p_worker,
        lease_expires_at=pg_catalog.now()+interval '3650 days',updated_at=pg_catalog.now()
      WHERE url=p_url AND lease_owner=p_worker AND worker_lease_id=lease.lease_id;
    ELSIF p_lane='linkedin' THEN
      UPDATE public.linkedin_queue SET apply_status='challenge_pending',
        est_cost_usd=LEAST(GREATEST(COALESCE((p_evidence->>'est_cost_usd')::numeric,0),0),100),
        agent_model=pg_catalog.left(p_evidence->>'agent_model',200),
        apply_duration_ms=(p_evidence->>'apply_duration_ms')::integer,worker_id=p_worker,
        lease_expires_at=pg_catalog.now()+interval '3650 days',updated_at=pg_catalog.now()
      WHERE url=p_url AND lease_owner=p_worker AND worker_lease_id=lease.lease_id;
      UPDATE public.rate_governor SET halted_until=pg_catalog.now()
        +pg_catalog.make_interval(secs=>GREATEST(COALESCE(p_halt_seconds,0),0)),
        updated_at=pg_catalog.now() WHERE scope_key='account:linkedin';
    ELSE RAISE EXCEPTION 'unsupported worker lane' USING ERRCODE='check_violation'; END IF;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    INSERT INTO public.auth_challenge(url,worker_id,machine_owner,home_ip,kind,route,screenshot_url)
    VALUES(p_url,p_worker,COALESCE(
             (SELECT w.machine_owner FROM public.workers w WHERE w.worker_id=p_worker),
             pg_catalog.left(p_evidence->>'machine_owner',200)),
           lease.home_ip,pg_catalog.left(p_kind,100),pg_catalog.left(p_evidence->>'challenge_route',100),
           pg_catalog.left(p_evidence->>'screenshot_url',1000));
    IF p_lane='ats' AND p_evidence->>'outcome' IN ('captcha','block') THEN
      UPDATE public.rate_governor SET
        captcha_24h=captcha_24h+CASE WHEN p_evidence->>'outcome'='captcha' THEN 1 ELSE 0 END,
        block_24h=block_24h+CASE WHEN p_evidence->>'outcome'='block' THEN 1 ELSE 0 END,
        last_attempt_at=pg_catalog.now(),updated_at=pg_catalog.now()
      WHERE scope_key IN ('global','home_ip:'||lease.home_ip,'host:'||lease.target_host);
      IF NOT FOUND THEN RAISE EXCEPTION 'required worker governor disappeared'
        USING ERRCODE='object_not_in_prerequisite_state'; END IF;
    END IF;
    INSERT INTO public.apply_result_events(
      queue_name,url,worker_id,machine_owner,status,apply_status,apply_error,target_host,home_ip,
      agent,agent_model,est_cost_usd,apply_duration_ms,application_tool_calls,job_log_path,
      transcript_digest,final_result_source,result_metadata,result_line,source,route,failure_class,
      tool_calls_total,last_tool,host_policy,evidence_is_assertion)
    SELECT CASE p_lane WHEN 'ats' THEN 'apply_queue' ELSE 'linkedin_queue' END,p_url,p_worker,
      COALESCE(w.machine_owner,pg_catalog.left(p_evidence->>'machine_owner',200)),
      'challenge_pending','challenge_pending',pg_catalog.left(p_kind,200),lease.target_host,
      lease.home_ip,pg_catalog.left(p_evidence->>'agent',100),pg_catalog.left(p_evidence->>'agent_model',200),
      LEAST(GREATEST(COALESCE((p_evidence->>'est_cost_usd')::numeric,0),0),100),
      (p_evidence->>'apply_duration_ms')::integer,(p_evidence->>'application_tool_calls')::integer,
      pg_catalog.left(p_evidence->>'job_log_path',1000),pg_catalog.left(p_evidence->>'transcript_digest',1000),
      pg_catalog.left(p_evidence->>'final_result_source',200),COALESCE(p_evidence->'result_metadata','{}'::jsonb),
      'RESULT:CHALLENGE_PENDING','worker',pg_catalog.left(p_evidence->>'route',100),
      pg_catalog.left(p_evidence->>'failure_class',200),(p_evidence->>'tool_calls_total')::integer,
      pg_catalog.left(p_evidence->>'last_tool',200),pg_catalog.left(p_evidence->>'host_policy',200),TRUE
    FROM (SELECT 1) seed LEFT JOIN public.workers w ON w.worker_id=p_worker;
    IF COALESCE((p_evidence->>'est_cost_usd')::numeric,0)>0 THEN
      INSERT INTO public.llm_usage(worker_id,machine_owner,task,model,provider,cost_usd)
      VALUES(p_worker,p_evidence->>'machine_owner','apply_agent',
        pg_catalog.left(p_evidence->>'agent_model',200),pg_catalog.left(p_evidence->>'agent',100),
        LEAST((p_evidence->>'est_cost_usd')::numeric,100));
    END IF;
    UPDATE public.fleet_worker_lease_ledger SET state='parked' WHERE lease_id=lease.lease_id;
    RETURN TRUE;
END
$fleet_worker_park$;

REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_lease_ats(TEXT,TEXT,INTEGER,TEXT,INTEGER) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_lease_linkedin(TEXT,TEXT,TEXT,INTEGER,TEXT) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_requeue(TEXT,TEXT,TEXT,TEXT) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_park_infrastructure(TEXT,TEXT,TEXT) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_terminalize(TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,JSONB) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_park(TEXT,TEXT,TEXT,TEXT,INTEGER,JSONB) FROM PUBLIC;

-- Preserve all-in spend across retries. est_cost_usd remains the latest/current
-- attempt value for diagnostics; cumulative_cost_usd is the cap/reporting ledger.
ALTER TABLE apply_queue
    ADD COLUMN IF NOT EXISTS cumulative_cost_usd NUMERIC(12,4) NOT NULL DEFAULT 0;
WITH event_costs AS MATERIALIZED (
    SELECT e.url, SUM(e.est_cost_usd) AS total_cost_usd
    FROM apply_result_events e
    WHERE e.queue_name = 'apply_queue'
    GROUP BY e.url
), desired AS MATERIALIZED (
    SELECT q.url, GREATEST(
        COALESCE(q.cumulative_cost_usd, 0),
        COALESCE(q.est_cost_usd, 0),
        COALESCE(e.total_cost_usd, 0)
    ) AS cumulative_cost_usd
    FROM apply_queue q
    LEFT JOIN event_costs e ON e.url = q.url
)
UPDATE apply_queue q
SET cumulative_cost_usd = desired.cumulative_cost_usd
FROM desired
WHERE q.url = desired.url
  AND q.cumulative_cost_usd IS DISTINCT FROM desired.cumulative_cost_usd;
ALTER TABLE apply_queue
    ALTER COLUMN cumulative_cost_usd SET DEFAULT 0,
    ALTER COLUMN cumulative_cost_usd SET NOT NULL;
ALTER TABLE linkedin_queue
    ADD COLUMN IF NOT EXISTS cumulative_cost_usd NUMERIC(12,4) NOT NULL DEFAULT 0;
WITH desired AS MATERIALIZED (
    SELECT q.url, GREATEST(
        COALESCE(q.cumulative_cost_usd, 0),
        COALESCE(q.est_cost_usd, 0)
    ) AS cumulative_cost_usd
    FROM linkedin_queue q
)
UPDATE linkedin_queue q
SET cumulative_cost_usd = desired.cumulative_cost_usd
FROM desired
WHERE q.url = desired.url
  AND q.cumulative_cost_usd IS DISTINCT FROM desired.cumulative_cost_usd;
ALTER TABLE linkedin_queue
    ALTER COLUMN cumulative_cost_usd SET DEFAULT 0,
    ALTER COLUMN cumulative_cost_usd SET NOT NULL;

-- H19/H13 (red-team): self-contained host_skip audit + recurrence linkage + breadth evidence on
-- the diagnosis row, so a Reverse / audit can report exactly which/how-many rows were affected,
-- how many prior incidents, and how broad the block was -- without re-deriving from transient state.
ALTER TABLE fleet_diagnoses ADD COLUMN IF NOT EXISTS rows_affected        INTEGER;
ALTER TABLE fleet_diagnoses ADD COLUMN IF NOT EXISTS prior_incident_count INTEGER;
ALTER TABLE fleet_diagnoses ADD COLUMN IF NOT EXISTS distinct_hosts       INTEGER;
ALTER TABLE fleet_diagnoses ADD COLUMN IF NOT EXISTS distinct_workers     INTEGER;

-- email_reconcile_actions: audit + reversibility for the email-verification reconcile.
CREATE TABLE IF NOT EXISTS email_reconcile_actions (
    id              BIGSERIAL PRIMARY KEY,
    url             TEXT,
    message_id      TEXT,
    match_method    TEXT,
    match_score     REAL,
    stage           TEXT,
    prior_status    TEXT,
    how_to_reverse  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- dedup_repair_actions: audit + reversibility for source-specific rewrites of
-- overbroad queued dedup keys (for example aggregator/missing-company keys).
CREATE TABLE IF NOT EXISTS dedup_repair_actions (
    id              BIGSERIAL PRIMARY KEY,
    url             TEXT,
    old_dedup_key   TEXT,
    new_dedup_key   TEXT,
    reason          TEXT,
    how_to_reverse  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dedup_repair_actions_old_key
    ON dedup_repair_actions (old_dedup_key, created_at);

-- ---------------------------------------------------------------------------
-- Authenticated worker API.  Login roles have EXECUTE only; controller tables
-- remain inaccessible even when a caller fabricates worker identifiers.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fleet_worker_admission_snapshot()
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_admission_snapshot$
DECLARE principal public.fleet_worker_principals%ROWTYPE; payload JSONB;
BEGIN
  SELECT * INTO principal FROM public.fleet_worker_principals WHERE role_name=session_user;
  IF NOT FOUND AND session_user=current_user THEN
    SELECT pg_catalog.jsonb_build_object(
      'schema_contract_version',3,'paused',c.paused,'ats_paused',c.ats_paused,
      'ats_apply_mode',c.ats_apply_mode,'linkedin_apply_mode',c.linkedin_apply_mode,
      'pinned_worker_version',c.pinned_worker_version,'linkedin_owner_ip',c.linkedin_owner_ip,
      'agent_timeout_override',c.agent_timeout_override,
      'global_should_halt',c.paused OR (c.spend_cap_usd>0 AND COALESCE((SELECT SUM(a.cumulative_cost_usd) FROM public.apply_queue a),0)>=c.spend_cap_usd),
      'ats_should_halt',c.paused OR c.ats_paused OR c.ats_apply_mode='stopped'
        OR (c.spend_cap_usd>0 AND COALESCE((SELECT SUM(a.cumulative_cost_usd) FROM public.apply_queue a),0)>=c.spend_cap_usd),
      'linkedin_should_halt',c.paused OR c.linkedin_apply_mode='stopped')
    INTO payload FROM public.fleet_config c WHERE c.id=1;
    RETURN payload;
  ELSIF NOT FOUND THEN
    RAISE EXCEPTION 'unmapped worker principal' USING ERRCODE='insufficient_privilege';
  END IF;
  IF pg_catalog.to_regclass('public.fleet_desired_state') IS NULL THEN
    RAISE EXCEPTION 'fleet enrollment control is unavailable' USING ERRCODE='object_not_in_prerequisite_state';
  END IF;
  EXECUTE $sql$
    SELECT pg_catalog.jsonb_build_object(
      'worker_id',w.worker_id,'machine_owner',w.machine_owner,'public_ip',w.public_ip,
      'validated',w.validated,'revoked_at',w.revoked_at,'capabilities',w.capabilities,
      'contract',$1,'desired_workers',d.desired_workers,'generation',d.generation,
      'desired_agent',d.agent,'desired_model',d.model,
      'desired_updated_at',d.updated_at,'paused',c.paused,'ats_paused',c.ats_paused,
      'ats_apply_mode',c.ats_apply_mode,'linkedin_apply_mode',c.linkedin_apply_mode,
      'ats_policy_version',c.ats_policy_version,'linkedin_policy_version',c.linkedin_policy_version,
      'linkedin_owner_ip',c.linkedin_owner_ip,
      'pinned_worker_version',c.pinned_worker_version,'canary_version',c.canary_version,
      'agent_timeout_override',c.agent_timeout_override,
      'heartbeat_last_beat',h.last_beat,'heartbeat_sw_version',h.sw_version,
      'schema_contract_version',3,
      'global_should_halt',c.paused OR (c.spend_cap_usd>0 AND COALESCE((SELECT SUM(a.cumulative_cost_usd) FROM public.apply_queue a),0)>=c.spend_cap_usd),
      'ats_should_halt',c.paused OR c.ats_paused OR c.ats_apply_mode='stopped'
        OR (c.spend_cap_usd>0 AND COALESCE((SELECT SUM(a.cumulative_cost_usd) FROM public.apply_queue a),0)>=c.spend_cap_usd),
      'linkedin_should_halt',c.paused OR c.linkedin_apply_mode='stopped',
      'admission_allowed',
        w.validated AND w.revoked_at IS NULL AND d.desired_workers>0
        AND d.updated_at>=pg_catalog.now()-interval '5 minutes'
        AND h.last_beat>=pg_catalog.now()-interval '90 seconds'
        AND h.sw_version=CASE WHEN c.canary_worker_id=w.worker_id
          THEN COALESCE(c.canary_version,c.pinned_worker_version) ELSE c.pinned_worker_version END
        AND NOT c.paused
        AND CASE $1
          WHEN 'apply' THEN NOT c.ats_paused AND c.ats_apply_mode IN ('canary','steady') AND c.ats_policy_version IS NOT NULL
          WHEN 'linkedin' THEN c.linkedin_apply_mode IN ('canary','steady') AND c.linkedin_policy_version IS NOT NULL
          ELSE TRUE END,
      'admission_reason',CASE
        WHEN NOT w.validated OR w.revoked_at IS NOT NULL THEN 'enrollment_inactive'
        WHEN d.desired_workers<=0 THEN 'desired_state_inactive'
        WHEN d.updated_at<pg_catalog.now()-interval '5 minutes' THEN 'desired_state_stale'
        WHEN h.last_beat IS NULL OR h.last_beat<pg_catalog.now()-interval '90 seconds' THEN 'heartbeat_stale'
        WHEN h.sw_version IS DISTINCT FROM CASE WHEN c.canary_worker_id=w.worker_id
          THEN COALESCE(c.canary_version,c.pinned_worker_version) ELSE c.pinned_worker_version END THEN 'version_mismatch'
        WHEN c.paused THEN 'global_paused'
        WHEN $1='apply' AND c.ats_paused THEN 'ats_paused'
        WHEN $1='apply' AND c.ats_apply_mode NOT IN ('canary','steady') THEN 'ats_stopped'
        WHEN $1='linkedin' AND c.linkedin_apply_mode NOT IN ('canary','steady') THEN 'linkedin_stopped'
        WHEN $1='apply' AND c.ats_policy_version IS NULL THEN 'ats_policy_missing'
        WHEN $1='linkedin' AND c.linkedin_policy_version IS NULL THEN 'linkedin_policy_missing'
        ELSE 'allowed' END)
    FROM public.workers w
    JOIN public.fleet_desired_state d ON d.machine_owner=w.machine_owner
    JOIN public.fleet_config c ON c.id=1
    LEFT JOIN public.worker_heartbeat h ON h.worker_id=w.worker_id
    WHERE w.worker_id=$2
    FOR SHARE OF w,d,c
  $sql$ INTO payload USING principal.contract,principal.worker_id;
  IF payload IS NULL THEN
    RAISE EXCEPTION 'worker enrollment is incomplete' USING ERRCODE='insufficient_privilege';
  END IF;
  RETURN payload;
END
$fleet_worker_admission_snapshot$;

CREATE OR REPLACE FUNCTION public.fleet_worker_schema_contract()
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_schema_contract$
DECLARE principal public.fleet_worker_principals%ROWTYPE; missing_tables TEXT[]; missing_columns TEXT[];
BEGIN
  SELECT * INTO principal FROM public.fleet_worker_principals WHERE role_name=session_user;
  IF NOT FOUND AND session_user<>current_user THEN
    RAISE EXCEPTION 'unmapped worker principal' USING ERRCODE='insufficient_privilege';
  END IF;
  SELECT pg_catalog.array_agg(name ORDER BY name) INTO missing_tables
  FROM pg_catalog.unnest(ARRAY[
    'agent_availability','answer_bank','applied_set','apply_attempts','apply_queue',
    'apply_result_events','auth_challenge','command_acks','compute_queue','discovered_postings',
    'fleet_assets','fleet_config','linkedin_queue','llm_usage','otp_request','rate_governor',
    'remote_commands','search_tasks','worker_heartbeat','workers'
  ]) name WHERE pg_catalog.to_regclass('public.'||name) IS NULL;
  SELECT pg_catalog.array_agg(spec ORDER BY spec) INTO missing_columns
  FROM pg_catalog.unnest(ARRAY[
    'apply_queue.cumulative_cost_usd','apply_result_events.result_metadata',
    'apply_result_events.evidence_is_assertion','apply_attempts.attempt_id',
    'apply_attempts.verification_ref','fleet_config.ats_apply_mode',
    'fleet_config.linkedin_apply_mode','linkedin_queue.worker_lease_id',
    'otp_request.wait_started_at','otp_request.code_kind'
  ]) spec
  WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute a
    WHERE a.attrelid=pg_catalog.to_regclass('public.'||pg_catalog.split_part(spec,'.',1))
      AND a.attname=pg_catalog.split_part(spec,'.',2) AND a.attnum>0 AND NOT a.attisdropped
  );
  RETURN pg_catalog.jsonb_build_object(
    'contract_version',3,'contract',principal.contract,
    'ready',COALESCE(pg_catalog.array_length(missing_tables,1),0)=0
      AND COALESCE(pg_catalog.array_length(missing_columns,1),0)=0,
    'missing_tables',COALESCE(pg_catalog.to_jsonb(missing_tables),'[]'::jsonb),
    'missing_columns',COALESCE(pg_catalog.to_jsonb(missing_columns),'[]'::jsonb),
    'apply_result_event_ready',NOT ('apply_result_events.result_metadata'=ANY(COALESCE(missing_columns,ARRAY[]::text[]))),
    'apply_attempt_ready',NOT ('apply_attempts.attempt_id'=ANY(COALESCE(missing_columns,ARRAY[]::text[])))
  );
END
$fleet_worker_schema_contract$;

CREATE OR REPLACE FUNCTION public.fleet_worker_heartbeat(p_telemetry JSONB)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_heartbeat$
DECLARE principal public.fleet_worker_principals%ROWTYPE; enrolled public.workers%ROWTYPE;
BEGIN
  IF pg_catalog.octet_length(COALESCE(p_telemetry,'{}'::jsonb)::text)>16384 THEN
    RAISE EXCEPTION 'worker telemetry exceeds limit' USING ERRCODE='program_limit_exceeded';
  END IF;
  SELECT * INTO principal FROM public.fleet_worker_principals WHERE role_name=session_user;
  IF FOUND THEN
    SELECT * INTO STRICT enrolled FROM public.workers WHERE worker_id=principal.worker_id FOR SHARE;
    IF NOT enrolled.validated OR enrolled.revoked_at IS NOT NULL THEN
      RAISE EXCEPTION 'worker enrollment is not active' USING ERRCODE='insufficient_privilege';
    END IF;
  ELSIF session_user=current_user THEN
    principal.worker_id:=NULLIF(pg_catalog.btrim(p_telemetry->>'worker_id'),'');
    principal.contract:=NULLIF(pg_catalog.btrim(p_telemetry->>'role'),'');
    enrolled.machine_owner:=p_telemetry->>'machine_owner';
    enrolled.public_ip:=p_telemetry->>'home_ip';
    IF principal.worker_id IS NULL THEN RAISE EXCEPTION 'controller heartbeat requires worker_id'
      USING ERRCODE='check_violation'; END IF;
  ELSE
    RAISE EXCEPTION 'unmapped worker principal' USING ERRCODE='insufficient_privilege';
  END IF;
  INSERT INTO public.worker_heartbeat(
    worker_id,machine_owner,home_ip,role,state,current_job,sw_version,last_error,recent_log,
    current_agent,current_model,agent_chain,last_agent_switch_at,last_agent_switch_reason,last_beat)
  VALUES(principal.worker_id,enrolled.machine_owner,enrolled.public_ip,principal.contract,
    pg_catalog.left(COALESCE(p_telemetry->>'state','idle'),40),
    pg_catalog.left(p_telemetry->>'current_job',1000),pg_catalog.left(p_telemetry->>'sw_version',200),
    pg_catalog.left(p_telemetry->>'last_error',4000),pg_catalog.left(p_telemetry->>'recent_log',8000),
    pg_catalog.left(p_telemetry->>'current_agent',100),pg_catalog.left(p_telemetry->>'current_model',200),
    pg_catalog.left(p_telemetry->>'agent_chain',1000),
    CASE WHEN p_telemetry ? 'last_agent_switch_at' THEN (p_telemetry->>'last_agent_switch_at')::timestamptz END,
    pg_catalog.left(p_telemetry->>'last_agent_switch_reason',500),pg_catalog.now())
  ON CONFLICT(worker_id) DO UPDATE SET
    machine_owner=EXCLUDED.machine_owner,home_ip=EXCLUDED.home_ip,role=EXCLUDED.role,
    state=EXCLUDED.state,current_job=EXCLUDED.current_job,
    sw_version=COALESCE(EXCLUDED.sw_version,public.worker_heartbeat.sw_version),
    last_error=EXCLUDED.last_error,recent_log=EXCLUDED.recent_log,current_agent=EXCLUDED.current_agent,
    current_model=EXCLUDED.current_model,agent_chain=EXCLUDED.agent_chain,
    last_agent_switch_at=COALESCE(EXCLUDED.last_agent_switch_at,public.worker_heartbeat.last_agent_switch_at),
    last_agent_switch_reason=COALESCE(EXCLUDED.last_agent_switch_reason,public.worker_heartbeat.last_agent_switch_reason),
    last_beat=pg_catalog.now();
  RETURN pg_catalog.jsonb_build_object('worker_id',principal.worker_id,'last_beat',pg_catalog.now());
END
$fleet_worker_heartbeat$;

DROP FUNCTION IF EXISTS public.fleet_worker_runtime_state();
CREATE OR REPLACE FUNCTION public.fleet_worker_runtime_state(p_worker TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_runtime_state$
DECLARE principal public.fleet_worker_principals%ROWTYPE; result JSONB; target_machine TEXT;
BEGIN
  SELECT * INTO principal FROM public.fleet_worker_principals WHERE role_name=session_user;
  IF FOUND THEN
    p_worker:=principal.worker_id;
    SELECT w.machine_owner INTO target_machine FROM public.workers w WHERE w.worker_id=p_worker;
  ELSIF session_user<>current_user THEN
    RAISE EXCEPTION 'unmapped worker principal' USING ERRCODE='insufficient_privilege';
  ELSE
    target_machine:=p_worker;
  END IF;
  SELECT pg_catalog.jsonb_build_object(
    'state',(SELECT h.state FROM public.worker_heartbeat h WHERE h.worker_id=p_worker),
    'last_agent_switch_reason',(SELECT h.last_agent_switch_reason FROM public.worker_heartbeat h WHERE h.worker_id=p_worker),
    'agents',COALESCE((SELECT pg_catalog.jsonb_agg(pg_catalog.to_jsonb(a)) FROM public.agent_availability a),'[]'::jsonb),
    'commands',COALESCE((SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
      'id',c.id,'worker_id',c.worker_id,'command',c.command,
      'target_version',c.target_version,'issued_at',c.issued_at))
      FROM public.remote_commands c WHERE c.acked_at IS NULL
        AND c.worker_id IN (p_worker,'*')
        AND NOT EXISTS (SELECT 1 FROM public.command_acks x
          WHERE x.command_id=c.id AND x.worker_id=p_worker)),'[]'::jsonb),
    'compute_context',CASE WHEN principal.contract='compute' OR session_user=current_user THEN COALESCE((
      SELECT pg_catalog.jsonb_object_agg(a.name,pg_catalog.encode(a.data,'base64'))
      FROM public.fleet_assets a WHERE a.name=ANY(ARRAY[
        'ctx:resume','ctx:preference','ctx:kg_prompt','ctx:search_cfg','ctx:version'
      ])), '{}'::jsonb) ELSE NULL END,
    'update_busy_reasons',COALESCE((
      SELECT pg_catalog.jsonb_agg(reason ORDER BY reason) FROM (
        SELECT 'heartbeat:'||h.worker_id||':'||h.state AS reason
        FROM public.worker_heartbeat h
        WHERE h.worker_id LIKE target_machine||'-%'
          AND h.state NOT IN ('idle','paused')
          AND h.last_beat>pg_catalog.now()-interval '150 seconds'
        UNION ALL
        SELECT 'apply_queue:live_leases:'||pg_catalog.count(*)::text
        FROM public.apply_queue q JOIN public.worker_heartbeat h ON h.worker_id=q.lease_owner
        WHERE q.lease_owner LIKE target_machine||'-%'
          AND q.status='leased' AND q.lease_expires_at>pg_catalog.now()
          AND q.lease_expires_at<pg_catalog.now()+interval '1 day'
          AND h.last_beat>pg_catalog.now()-interval '150 seconds' HAVING pg_catalog.count(*)>0
        UNION ALL
        SELECT 'linkedin_queue:live_leases:'||pg_catalog.count(*)::text
        FROM public.linkedin_queue q JOIN public.worker_heartbeat h ON h.worker_id=q.lease_owner
        WHERE q.lease_owner LIKE target_machine||'-%'
          AND q.status='leased' AND q.lease_expires_at>pg_catalog.now()
          AND q.lease_expires_at<pg_catalog.now()+interval '1 day'
          AND h.last_beat>pg_catalog.now()-interval '150 seconds' HAVING pg_catalog.count(*)>0
      ) busy), '[]'::jsonb))
  INTO result;
  RETURN COALESCE(result,pg_catalog.jsonb_build_object('state',NULL,'commands','[]'::jsonb,'agents','[]'::jsonb));
END
$fleet_worker_runtime_state$;

CREATE OR REPLACE FUNCTION public.fleet_worker_version_status(p_worker TEXT,p_reported TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_version_status$
DECLARE principal public.fleet_worker_principals%ROWTYPE; expected TEXT; actual TEXT;
BEGIN
  SELECT * INTO principal FROM public.fleet_worker_principals WHERE role_name=session_user;
  IF FOUND THEN
    p_worker:=principal.worker_id;
    SELECT h.sw_version INTO actual FROM public.worker_heartbeat h WHERE h.worker_id=p_worker;
  ELSIF session_user=current_user THEN actual:=p_reported;
  ELSE RAISE EXCEPTION 'unmapped worker principal' USING ERRCODE='insufficient_privilege'; END IF;
  SELECT CASE WHEN c.canary_worker_id=p_worker THEN COALESCE(c.canary_version,c.pinned_worker_version)
              ELSE c.pinned_worker_version END INTO expected
  FROM public.fleet_config c WHERE c.id=1 FOR SHARE;
  RETURN pg_catalog.jsonb_build_object('expected_version',expected,'sw_version',actual,
    'matches',expected IS NULL OR actual=expected);
END
$fleet_worker_version_status$;

CREATE OR REPLACE FUNCTION public.fleet_worker_claim_liveness()
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_claim_liveness$
DECLARE principal public.fleet_worker_principals%ROWTYPE; chosen public.apply_queue%ROWTYPE;
BEGIN
  SELECT * INTO STRICT principal FROM public.fleet_worker_principals
  WHERE role_name=session_user AND contract='apply';
  SELECT q.* INTO chosen FROM public.apply_queue q JOIN public.fleet_config c ON c.id=1
  WHERE q.status='queued' AND q.lane='ats' AND q.approved_batch IS NOT NULL
    AND q.score>=COALESCE(c.approval_threshold,7) AND q.liveness_required
    AND (q.liveness_checked_at IS NULL OR q.liveness_checked_at<pg_catalog.now()-interval '15 minutes')
    AND (q.liveness_check_owner IS NULL OR q.liveness_check_expires_at<pg_catalog.now())
  ORDER BY q.score DESC,q.url LIMIT 1 FOR UPDATE OF q SKIP LOCKED;
  IF NOT FOUND THEN RETURN NULL; END IF;
  UPDATE public.apply_queue SET liveness_check_owner=principal.worker_id,
    liveness_check_expires_at=pg_catalog.now()+interval '2 minutes',updated_at=pg_catalog.now()
  WHERE url=chosen.url;
  RETURN pg_catalog.jsonb_build_object('url',chosen.url,'application_url',chosen.application_url,
    'company',chosen.company,'title',chosen.title,'target_host',COALESCE(chosen.target_host,chosen.apply_domain));
END
$fleet_worker_claim_liveness$;

CREATE OR REPLACE FUNCTION public.fleet_worker_write_liveness(p_url TEXT,p_status TEXT,p_reason TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_write_liveness$
DECLARE principal public.fleet_worker_principals%ROWTYPE; normalized TEXT;
BEGIN
  SELECT * INTO STRICT principal FROM public.fleet_worker_principals
  WHERE role_name=session_user AND contract='apply';
  normalized:=CASE WHEN p_status IN ('live','dead','uncertain') THEN p_status ELSE 'uncertain' END;
  UPDATE public.apply_queue SET liveness_status=normalized,liveness_reason=pg_catalog.left(p_reason,200),
    liveness_checked_at=pg_catalog.now(),liveness_check_owner=NULL,liveness_check_expires_at=NULL,
    liveness_check_count=COALESCE(liveness_check_count,0)+1,
    liveness_consecutive_uncertain=CASE WHEN normalized='uncertain' THEN COALESCE(liveness_consecutive_uncertain,0)+1 ELSE 0 END,
    status=CASE WHEN normalized='dead' THEN 'failed'::public.apply_queue_status ELSE status END,
    apply_status=CASE WHEN normalized='dead' THEN 'expired' ELSE apply_status END,
    apply_error=CASE WHEN normalized='dead' THEN pg_catalog.left('liveness:'||COALESCE(p_reason,'dead'),200) ELSE apply_error END,
    updated_at=pg_catalog.now()
  WHERE url=p_url AND liveness_check_owner=principal.worker_id;
  RETURN FOUND;
END
$fleet_worker_write_liveness$;

CREATE OR REPLACE FUNCTION public.fleet_worker_agent_blocks()
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_agent_blocks$
BEGIN
  PERFORM 1 FROM public.fleet_worker_principals WHERE role_name=session_user;
  IF NOT FOUND AND session_user<>current_user THEN
    RAISE EXCEPTION 'unmapped worker principal' USING ERRCODE='insufficient_privilege'; END IF;
  RETURN COALESCE((SELECT pg_catalog.jsonb_object_agg(a.agent,a.blocked_until)
    FROM public.agent_availability a WHERE a.blocked_until>pg_catalog.now()),'{}'::jsonb);
END
$fleet_worker_agent_blocks$;

CREATE OR REPLACE FUNCTION public.fleet_worker_record_agent_wall(p_agent TEXT,p_blocked_until TIMESTAMPTZ)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_record_agent_wall$
BEGIN
  PERFORM 1 FROM public.fleet_worker_principals WHERE role_name=session_user AND contract IN ('apply','linkedin');
  IF NOT FOUND THEN
    RAISE EXCEPTION 'unmapped apply principal' USING ERRCODE='insufficient_privilege'; END IF;
  IF NULLIF(pg_catalog.btrim(p_agent),'') IS NULL OR pg_catalog.length(p_agent)>50 THEN
    RAISE EXCEPTION 'invalid agent assertion' USING ERRCODE='check_violation'; END IF;
  p_blocked_until:=LEAST(GREATEST(p_blocked_until,pg_catalog.now()),pg_catalog.now()+interval '6 hours');
  INSERT INTO public.agent_availability(agent,blocked_until,reason,updated_at)
  VALUES(p_agent,p_blocked_until,'usage_limit_wall',pg_catalog.now())
  ON CONFLICT(agent) DO UPDATE SET blocked_until=GREATEST(public.agent_availability.blocked_until,EXCLUDED.blocked_until),
    reason='usage_limit_wall',updated_at=pg_catalog.now();
  RETURN TRUE;
END
$fleet_worker_record_agent_wall$;

CREATE OR REPLACE FUNCTION public.fleet_worker_evaluate_agent_budget(
  p_caps JSONB,p_window_seconds INTEGER,p_cooldown_seconds INTEGER
) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_evaluate_agent_budget$
DECLARE principal_contract TEXT; item RECORD; cap NUMERIC; spend NUMERIC; blocked TIMESTAMPTZ;
  result JSONB:='{}'::jsonb;
BEGIN
  SELECT p.contract INTO principal_contract FROM public.fleet_worker_principals p
  WHERE p.role_name=session_user;
  IF FOUND THEN
    IF principal_contract NOT IN ('apply','linkedin') THEN
      RAISE EXCEPTION 'worker principal cannot evaluate apply-agent budget'
        USING ERRCODE='insufficient_privilege';
    END IF;
  ELSIF session_user<>current_user THEN
    RAISE EXCEPTION 'unmapped worker principal' USING ERRCODE='insufficient_privilege';
  END IF;
  p_window_seconds:=LEAST(GREATEST(COALESCE(p_window_seconds,18000),60),86400);
  p_cooldown_seconds:=LEAST(GREATEST(COALESCE(p_cooldown_seconds,1800),60),86400);
  IF pg_catalog.jsonb_typeof(COALESCE(p_caps,'{}'::jsonb))<>'object' THEN
    RAISE EXCEPTION 'agent caps must be an object' USING ERRCODE='check_violation';
  END IF;
  FOR item IN SELECT key,value FROM pg_catalog.jsonb_each(COALESCE(p_caps,'{}'::jsonb)) LOOP
    IF pg_catalog.length(pg_catalog.btrim(item.key)) NOT BETWEEN 1 AND 50
       OR pg_catalog.jsonb_typeof(item.value)<>'number' THEN CONTINUE; END IF;
    cap:=LEAST(GREATEST((item.value#>>'{}')::numeric,0),10000);
    IF cap<=0 THEN CONTINUE; END IF;
    SELECT COALESCE(pg_catalog.sum(u.cost_usd),0) INTO spend FROM public.llm_usage u
    WHERE u.provider=item.key
      AND u.ts>pg_catalog.now()-pg_catalog.make_interval(secs=>p_window_seconds);
    IF spend>=cap THEN
      INSERT INTO public.agent_availability(agent,blocked_until,reason,updated_at)
      VALUES(item.key,pg_catalog.now()+pg_catalog.make_interval(secs=>p_cooldown_seconds),
        'predictive_spend',pg_catalog.now())
      ON CONFLICT(agent) DO UPDATE SET
        reason=CASE WHEN EXCLUDED.blocked_until>=public.agent_availability.blocked_until
          THEN EXCLUDED.reason ELSE public.agent_availability.reason END,
        blocked_until=GREATEST(EXCLUDED.blocked_until,public.agent_availability.blocked_until),
        updated_at=pg_catalog.now()
      RETURNING blocked_until INTO blocked;
      result:=result||pg_catalog.jsonb_build_object(item.key,blocked);
    END IF;
  END LOOP;
  RETURN result;
END
$fleet_worker_evaluate_agent_budget$;

DROP FUNCTION IF EXISTS public.fleet_worker_ack_command(BIGINT);
CREATE OR REPLACE FUNCTION public.fleet_worker_ack_command(p_command_id BIGINT,p_worker TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_ack_command$
DECLARE principal public.fleet_worker_principals%ROWTYPE; direct_command BOOLEAN;
BEGIN
  SELECT * INTO principal FROM public.fleet_worker_principals WHERE role_name=session_user;
  IF FOUND THEN p_worker:=principal.worker_id;
  ELSIF session_user<>current_user THEN
    RAISE EXCEPTION 'unmapped worker principal' USING ERRCODE='insufficient_privilege';
  END IF;
  SELECT worker_id=p_worker INTO direct_command FROM public.remote_commands
  WHERE id=p_command_id AND acked_at IS NULL AND worker_id IN (p_worker,'*') FOR UPDATE;
  IF NOT FOUND THEN RETURN FALSE; END IF;
  INSERT INTO public.command_acks(command_id,worker_id) VALUES(p_command_id,p_worker)
  ON CONFLICT DO NOTHING;
  IF direct_command THEN UPDATE public.remote_commands SET acked_at=pg_catalog.now() WHERE id=p_command_id; END IF;
  RETURN TRUE;
END
$fleet_worker_ack_command$;

CREATE OR REPLACE FUNCTION public.fleet_worker_attempt_create(
  p_route TEXT,p_route_version TEXT,p_evidence JSONB DEFAULT '{}'::jsonb)
RETURNS UUID LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_attempt_create$
DECLARE principal public.fleet_worker_principals%ROWTYPE; lease public.fleet_worker_lease_ledger%ROWTYPE;
  attempt UUID:=pg_catalog.gen_random_uuid(); dedup TEXT; qname TEXT;
BEGIN
  IF pg_catalog.octet_length(COALESCE(p_evidence,'{}'::jsonb)::text)>8192 THEN
    RAISE EXCEPTION 'attempt evidence exceeds limit' USING ERRCODE='program_limit_exceeded';
  END IF;
  SELECT * INTO STRICT principal FROM public.fleet_worker_principals
  WHERE role_name=session_user AND contract IN ('apply','linkedin');
  SELECT * INTO STRICT lease FROM public.fleet_worker_lease_ledger
  WHERE lease_id=NULLIF(pg_catalog.current_setting('applypilot.worker_lease_id',TRUE),'')::uuid
    AND worker_id=principal.worker_id AND state='leased' FOR UPDATE;
  IF lease.lane='ats' THEN SELECT dedup_key INTO dedup FROM public.apply_queue WHERE url=lease.url AND worker_lease_id=lease.lease_id; qname:='apply_queue';
  ELSE SELECT dedup_key INTO dedup FROM public.linkedin_queue WHERE url=lease.url AND worker_lease_id=lease.lease_id; qname:='linkedin_queue'; END IF;
  INSERT INTO public.apply_attempts(attempt_id,queue_name,url,dedup_key,worker_id,route,route_version,state,evidence)
  VALUES(attempt,qname,lease.url,dedup,principal.worker_id,pg_catalog.left(p_route,100),
    pg_catalog.left(p_route_version,200),'prepared',COALESCE(p_evidence,'{}'::jsonb));
  RETURN attempt;
END
$fleet_worker_attempt_create$;

CREATE OR REPLACE FUNCTION public.fleet_worker_attempt_transition(
  p_attempt UUID,p_expected TEXT,p_state TEXT,p_evidence JSONB DEFAULT '{}'::jsonb)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_attempt_transition$
DECLARE principal public.fleet_worker_principals%ROWTYPE; changed public.apply_attempts%ROWTYPE;
BEGIN
  SELECT * INTO STRICT principal FROM public.fleet_worker_principals WHERE role_name=session_user;
  IF NOT ((p_expected='prepared' AND p_state IN ('submit_started','failed_pre_submit')) OR
          (p_expected='submit_started' AND p_state IN ('submitted_unverified','quarantined'))) THEN
    RAISE EXCEPTION 'worker attempt transition is not permitted' USING ERRCODE='check_violation';
  END IF;
  IF pg_catalog.octet_length(COALESCE(p_evidence,'{}'::jsonb)::text)>8192 THEN
    RAISE EXCEPTION 'attempt evidence exceeds limit' USING ERRCODE='program_limit_exceeded';
  END IF;
  UPDATE public.apply_attempts SET state=p_state,
    submit_started_at=CASE WHEN p_state='submit_started' THEN COALESCE(submit_started_at,pg_catalog.now()) ELSE submit_started_at END,
    finalized_at=CASE WHEN p_state IN ('quarantined','failed_pre_submit') THEN pg_catalog.now() ELSE finalized_at END,
    evidence=evidence||COALESCE(p_evidence,'{}'::jsonb)
  WHERE attempt_id=p_attempt AND worker_id=principal.worker_id AND state=p_expected RETURNING * INTO changed;
  IF NOT FOUND THEN RAISE EXCEPTION 'attempt is not in expected worker-owned state' USING ERRCODE='serialization_failure'; END IF;
  IF p_state='submit_started' THEN
    UPDATE public.fleet_worker_lease_ledger SET browser_interaction_at=COALESCE(browser_interaction_at,pg_catalog.now())
    WHERE lane=CASE changed.queue_name WHEN 'apply_queue' THEN 'ats' ELSE 'linkedin' END
      AND url=changed.url AND worker_id=principal.worker_id AND state='leased';
  END IF;
  RETURN pg_catalog.jsonb_build_object('attempt_id',changed.attempt_id,'state',changed.state,
    'url',changed.url,'queue_name',changed.queue_name);
END
$fleet_worker_attempt_transition$;

CREATE OR REPLACE FUNCTION public.fleet_worker_lease_compute()
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_lease_compute$
DECLARE principal public.fleet_worker_principals%ROWTYPE; chosen public.compute_queue%ROWTYPE;
  admitted BOOLEAN:=FALSE;
BEGIN
  SELECT * INTO STRICT principal FROM public.fleet_worker_principals WHERE role_name=session_user AND contract='compute';
  IF pg_catalog.to_regclass('public.fleet_desired_state') IS NULL THEN RETURN NULL; END IF;
  EXECUTE $admission$
    SELECT TRUE FROM public.workers w
    JOIN public.worker_heartbeat h USING(worker_id)
    JOIN public.fleet_desired_state d ON d.machine_owner=w.machine_owner
    JOIN public.fleet_config c ON c.id=1
    WHERE w.worker_id=$1 AND w.validated AND w.revoked_at IS NULL
      AND d.desired_workers>0 AND d.updated_at>=pg_catalog.now()-interval '5 minutes'
      AND NOT c.paused AND h.role='compute'
      AND h.last_beat>=pg_catalog.now()-interval '90 seconds'
      AND h.sw_version IS NOT DISTINCT FROM CASE
        WHEN c.canary_worker_id=w.worker_id THEN COALESCE(c.canary_version,c.pinned_worker_version)
        ELSE c.pinned_worker_version END
    FOR SHARE OF w,h,d,c
  $admission$ INTO admitted USING principal.worker_id;
  IF admitted IS NOT TRUE THEN RETURN NULL; END IF;
  IF EXISTS(SELECT 1 FROM public.fleet_config c WHERE c.id=1 AND
    ((c.cost_cap_daily_usd>0 AND (SELECT COALESCE(SUM(u.cost_usd),0) FROM public.llm_usage u WHERE u.ts>=pg_catalog.now()-interval '24 hours')>=c.cost_cap_daily_usd)
      OR (c.cost_cap_total_usd>0 AND (SELECT COALESCE(SUM(u.cost_usd),0) FROM public.llm_usage u)>=c.cost_cap_total_usd))) THEN RETURN NULL; END IF;
  SELECT * INTO chosen FROM public.compute_queue WHERE status='queued'
  ORDER BY updated_at,url,task LIMIT 1 FOR UPDATE SKIP LOCKED;
  IF NOT FOUND THEN RETURN NULL; END IF;
  UPDATE public.compute_queue SET status='leased',lease_owner=principal.worker_id,
    lease_expires_at=pg_catalog.now()+interval '20 minutes',attempts=attempts+1,updated_at=pg_catalog.now()
  WHERE url=chosen.url AND task=chosen.task RETURNING * INTO chosen;
  RETURN pg_catalog.jsonb_build_object('url',chosen.url,'task',chosen.task,'payload',chosen.payload,'attempts',chosen.attempts);
END
$fleet_worker_lease_compute$;

CREATE OR REPLACE FUNCTION public.fleet_worker_complete_compute(
  p_url TEXT,p_task TEXT,p_status TEXT,p_result JSONB,p_usage JSONB DEFAULT '{}'::jsonb)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_complete_compute$
DECLARE principal public.fleet_worker_principals%ROWTYPE; cost NUMERIC;
BEGIN
  SELECT * INTO STRICT principal FROM public.fleet_worker_principals WHERE role_name=session_user AND contract='compute';
  IF p_status NOT IN ('done','failed','quarantined') OR pg_catalog.octet_length(COALESCE(p_result,'{}'::jsonb)::text)>262144 THEN
    RAISE EXCEPTION 'invalid compute completion' USING ERRCODE='check_violation'; END IF;
  cost:=LEAST(GREATEST(COALESCE((p_usage->>'cost_usd')::numeric,0),0),100);
  UPDATE public.compute_queue SET status=p_status::public.fleet_task_status,result=p_result,
    est_cost_usd=cost,lease_owner=NULL,lease_expires_at=NULL,updated_at=pg_catalog.now()
  WHERE url=p_url AND task=p_task AND status='leased' AND lease_owner=principal.worker_id;
  IF NOT FOUND THEN RETURN FALSE; END IF;
  INSERT INTO public.llm_usage(worker_id,machine_owner,task,model,provider,tokens_in,tokens_out,cost_usd)
  SELECT principal.worker_id,w.machine_owner,p_task,pg_catalog.left(p_usage->>'model',200),
    pg_catalog.left(p_usage->>'provider',100),(p_usage->>'tokens_in')::integer,
    (p_usage->>'tokens_out')::integer,cost FROM public.workers w WHERE w.worker_id=principal.worker_id;
  RETURN TRUE;
END
$fleet_worker_complete_compute$;

CREATE OR REPLACE FUNCTION public.fleet_worker_lease_search()
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_lease_search$
DECLARE principal public.fleet_worker_principals%ROWTYPE; chosen public.search_tasks%ROWTYPE;
  admitted BOOLEAN:=FALSE;
BEGIN
  SELECT * INTO STRICT principal FROM public.fleet_worker_principals WHERE role_name=session_user AND contract='discovery';
  IF pg_catalog.to_regclass('public.fleet_desired_state') IS NULL THEN RETURN NULL; END IF;
  EXECUTE $admission$
    SELECT TRUE FROM public.workers w
    JOIN public.worker_heartbeat h USING(worker_id)
    JOIN public.fleet_desired_state d ON d.machine_owner=w.machine_owner
    JOIN public.fleet_config c ON c.id=1
    WHERE w.worker_id=$1 AND w.validated AND w.revoked_at IS NULL
      AND d.desired_workers>0 AND d.updated_at>=pg_catalog.now()-interval '5 minutes'
      AND NOT c.paused AND h.role='discovery'
      AND h.last_beat>=pg_catalog.now()-interval '90 seconds'
      AND h.sw_version IS NOT DISTINCT FROM CASE
        WHEN c.canary_worker_id=w.worker_id THEN COALESCE(c.canary_version,c.pinned_worker_version)
        ELSE c.pinned_worker_version END
    FOR SHARE OF w,h,d,c
  $admission$ INTO admitted USING principal.worker_id;
  IF admitted IS NOT TRUE THEN RETURN NULL; END IF;
  SELECT s.* INTO chosen FROM public.search_tasks s JOIN public.rate_governor g ON g.scope_key='board:'||s.board
  WHERE s.status='queued' AND s.enabled AND s.next_due_at<=pg_catalog.now()
    AND g.breaker_state<>'demoted' AND NOT(g.breaker_state='paused' AND COALESCE(g.breaker_until,'infinity')>=pg_catalog.now())
    AND g.count_24h<g.daily_cap AND (COALESCE(g.last_applied_at,g.last_attempt_at) IS NULL
      OR COALESCE(g.last_applied_at,g.last_attempt_at)<pg_catalog.now()-pg_catalog.make_interval(secs=>g.min_gap_seconds))
  ORDER BY s.next_due_at LIMIT 1 FOR UPDATE OF s,g SKIP LOCKED;
  IF NOT FOUND THEN RETURN NULL; END IF;
  UPDATE public.search_tasks SET status='leased',lease_owner=principal.worker_id,
    lease_expires_at=pg_catalog.now()+interval '15 minutes',attempts=attempts+1,updated_at=pg_catalog.now()
  WHERE task_id=chosen.task_id RETURNING * INTO chosen;
  RETURN pg_catalog.jsonb_build_object('task_id',chosen.task_id,'query',chosen.query,'board',chosen.board,
    'location',chosen.location,'params',chosen.params,'cadence_seconds',chosen.cadence_seconds);
END
$fleet_worker_lease_search$;

CREATE OR REPLACE FUNCTION public.fleet_worker_complete_search(
  p_task_id TEXT,p_postings JSONB,p_error TEXT DEFAULT NULL)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_complete_search$
DECLARE principal public.fleet_worker_principals%ROWTYPE; task public.search_tasks%ROWTYPE; posting JSONB; n INTEGER:=0;
BEGIN
  SELECT * INTO STRICT principal FROM public.fleet_worker_principals WHERE role_name=session_user AND contract='discovery';
  IF pg_catalog.jsonb_typeof(COALESCE(p_postings,'[]'::jsonb))<>'array' OR pg_catalog.jsonb_array_length(COALESCE(p_postings,'[]'::jsonb))>500 THEN
    RAISE EXCEPTION 'invalid discovery result batch' USING ERRCODE='program_limit_exceeded'; END IF;
  SELECT * INTO task FROM public.search_tasks WHERE task_id=p_task_id AND status='leased'
    AND lease_owner=principal.worker_id FOR UPDATE;
  IF NOT FOUND THEN RETURN FALSE; END IF;
  FOR posting IN SELECT value FROM pg_catalog.jsonb_array_elements(COALESCE(p_postings,'[]'::jsonb)) LOOP
    IF pg_catalog.octet_length(posting::text)>65536 THEN RAISE EXCEPTION 'posting exceeds limit' USING ERRCODE='program_limit_exceeded'; END IF;
    INSERT INTO public.discovered_postings(task_id,source_label,posting,worker_id)
    VALUES(task.task_id,task.board,posting,principal.worker_id); n:=n+1;
  END LOOP;
  UPDATE public.search_tasks SET status='queued',lease_owner=NULL,lease_expires_at=NULL,last_run_at=pg_catalog.now(),
    result_count=n,last_error=pg_catalog.left(p_error,500),next_due_at=pg_catalog.now()+pg_catalog.make_interval(secs=>cadence_seconds),updated_at=pg_catalog.now()
  WHERE task_id=task.task_id;
  UPDATE public.rate_governor SET count_24h=count_24h+CASE WHEN p_error IS NULL THEN 1 ELSE 0 END,
    success_24h=success_24h+CASE WHEN p_error IS NULL THEN 1 ELSE 0 END,
    captcha_24h=captcha_24h+CASE WHEN p_error='captcha' THEN 1 ELSE 0 END,
    block_24h=block_24h+CASE WHEN p_error='blocked' THEN 1 ELSE 0 END,
    last_attempt_at=pg_catalog.now(),last_applied_at=CASE WHEN p_error IS NULL THEN pg_catalog.now() ELSE last_applied_at END,
    updated_at=pg_catalog.now() WHERE scope_key='board:'||task.board;
  IF NOT FOUND THEN RAISE EXCEPTION 'required board governor disappeared' USING ERRCODE='object_not_in_prerequisite_state'; END IF;
  RETURN TRUE;
END
$fleet_worker_complete_search$;

CREATE OR REPLACE FUNCTION public.fleet_controller_verify_submission(
  p_lane TEXT,p_url TEXT,p_evidence_ref TEXT,p_verification_method TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_controller_verify_submission$
DECLARE lease public.fleet_worker_lease_ledger%ROWTYPE; dedup TEXT; company TEXT; applied_url TEXT;
BEGIN
  IF EXISTS(SELECT 1 FROM public.fleet_worker_principals WHERE role_name=session_user) THEN
    RAISE EXCEPTION 'worker principals cannot verify submissions' USING ERRCODE='insufficient_privilege'; END IF;
  IF NULLIF(pg_catalog.btrim(p_evidence_ref),'') IS NULL OR pg_catalog.length(p_evidence_ref)>1000
     OR NULLIF(pg_catalog.btrim(p_verification_method),'') IS NULL OR pg_catalog.length(p_verification_method)>100 THEN
    RAISE EXCEPTION 'independent verification evidence is required' USING ERRCODE='check_violation'; END IF;
  SELECT * INTO lease FROM public.fleet_worker_lease_ledger WHERE lane=p_lane AND url=p_url AND state='terminal'
    ORDER BY leased_at DESC LIMIT 1 FOR UPDATE;
  IF NOT FOUND THEN RETURN FALSE; END IF;
  IF p_lane='ats' THEN
    UPDATE public.apply_queue q SET status='applied',apply_status='applied',applied_at=pg_catalog.now(),updated_at=pg_catalog.now()
    WHERE q.url=p_url AND q.worker_lease_id=lease.lease_id AND q.status='crash_unconfirmed'
      AND q.apply_status='submission_claimed_unverified' RETURNING q.dedup_key,q.company,q.application_url INTO dedup,company,applied_url;
  ELSIF p_lane='linkedin' THEN
    UPDATE public.linkedin_queue q SET status='applied',apply_status='applied',applied_at=pg_catalog.now(),updated_at=pg_catalog.now()
    WHERE q.url=p_url AND q.worker_lease_id=lease.lease_id AND q.status='crash_unconfirmed'
      AND q.apply_status='submission_claimed_unverified' RETURNING q.dedup_key,q.company,q.application_url INTO dedup,company,applied_url;
  ELSE RAISE EXCEPTION 'invalid verification lane' USING ERRCODE='check_violation'; END IF;
  IF NOT FOUND THEN RETURN FALSE; END IF;
  IF dedup IS NOT NULL THEN INSERT INTO public.applied_set(dedup_key,company,applied_url)
    VALUES(dedup,company,COALESCE(applied_url,p_url)) ON CONFLICT DO NOTHING; END IF;
  IF p_lane='ats' THEN
    UPDATE public.rate_governor SET success_24h=success_24h+1,
      last_applied_at=pg_catalog.now(),last_attempt_at=pg_catalog.now(),updated_at=pg_catalog.now()
    WHERE scope_key IN ('global','host:'||lease.target_host,'home_ip:'||lease.home_ip);
  ELSE
    UPDATE public.rate_governor SET success_24h=success_24h+1,last_attempt_at=pg_catalog.now(),
      updated_at=pg_catalog.now() WHERE scope_key IN ('account:linkedin','global');
  END IF;
  IF NOT FOUND THEN RAISE EXCEPTION 'required verification governor disappeared'
    USING ERRCODE='object_not_in_prerequisite_state'; END IF;
  INSERT INTO public.apply_result_events(queue_name,url,worker_id,status,apply_status,result_line,source,
    final_result_source,result_metadata,evidence_is_assertion)
  VALUES(CASE p_lane WHEN 'ats' THEN 'apply_queue' ELSE 'linkedin_queue' END,p_url,lease.worker_id,
    'applied','applied','RESULT:APPLIED_VERIFIED','controller_verifier',p_verification_method,
    pg_catalog.jsonb_build_object('evidence_ref',p_evidence_ref),FALSE);
  UPDATE public.apply_attempts SET state='verified',finalized_at=pg_catalog.now(),
    verification_method=p_verification_method,verification_ref=p_evidence_ref
  WHERE queue_name=CASE p_lane WHEN 'ats' THEN 'apply_queue' ELSE 'linkedin_queue' END
    AND url=p_url AND worker_id=lease.worker_id AND state='submitted_unverified';
  RETURN TRUE;
END
$fleet_controller_verify_submission$;

CREATE OR REPLACE FUNCTION public.fleet_worker_otp_request(
  p_worker TEXT,p_job_url TEXT,p_application_url TEXT,p_ttl_seconds INTEGER)
RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_otp_request$
DECLARE principal public.fleet_worker_principals%ROWTYPE; lease public.fleet_worker_lease_ledger%ROWTYPE;
  target TEXT; expected_target TEXT; request_id BIGINT; ttl INTEGER;
BEGIN
  SELECT * INTO principal FROM public.fleet_worker_principals WHERE role_name=session_user;
  IF FOUND THEN
    IF principal.contract NOT IN ('apply','linkedin') THEN
      RAISE EXCEPTION 'OTP is unavailable for this worker contract' USING ERRCODE='insufficient_privilege'; END IF;
    p_worker:=principal.worker_id;
    SELECT * INTO STRICT lease FROM public.fleet_worker_lease_ledger
    WHERE lease_id=NULLIF(pg_catalog.current_setting('applypilot.worker_lease_id',TRUE),'')::uuid
      AND worker_id=principal.worker_id AND state='leased' AND url=p_job_url FOR SHARE;
    IF lease.lane='ats' THEN
      SELECT q.application_url INTO expected_target FROM public.apply_queue q
      WHERE q.url=lease.url AND q.worker_lease_id=lease.lease_id;
    ELSE
      SELECT q.application_url INTO expected_target FROM public.linkedin_queue q
      WHERE q.url=lease.url AND q.worker_lease_id=lease.lease_id;
    END IF;
  ELSIF session_user<>current_user THEN
    RAISE EXCEPTION 'unmapped worker principal' USING ERRCODE='insufficient_privilege';
  END IF;
  ttl:=LEAST(GREATEST(COALESCE(p_ttl_seconds,300),1),3600);
  target:=COALESCE(NULLIF(p_application_url,''),p_job_url);
  IF NULLIF(pg_catalog.btrim(p_worker),'') IS NULL OR NULLIF(pg_catalog.btrim(target),'') IS NULL
     OR pg_catalog.length(target)>2000 THEN
    RAISE EXCEPTION 'invalid OTP request identity or target' USING ERRCODE='check_violation'; END IF;
  IF principal.role_name IS NOT NULL AND target IS DISTINCT FROM COALESCE(expected_target,p_job_url) THEN
    RAISE EXCEPTION 'OTP target is not bound to the active lease' USING ERRCODE='insufficient_privilege'; END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_worker||E'\n'||target,0));
  SELECT o.id INTO request_id FROM public.otp_request o
  WHERE o.worker_id=p_worker AND o.url=target AND o.consumed_at IS NULL
    AND o.expires_at>pg_catalog.now() ORDER BY o.requested_at DESC,o.id DESC LIMIT 1 FOR UPDATE;
  IF FOUND THEN
    UPDATE public.otp_request SET expires_at=CASE WHEN code IS NULL
      THEN GREATEST(expires_at,pg_catalog.now()+pg_catalog.make_interval(secs=>ttl))
      ELSE expires_at END WHERE id=request_id;
  ELSE
    INSERT INTO public.otp_request(worker_id,url,sender_hint,expires_at)
    VALUES(p_worker,target,pg_catalog.lower(COALESCE(pg_catalog.split_part(
      pg_catalog.regexp_replace(target,'^[a-zA-Z][a-zA-Z0-9+.-]*://',''), '/',1),'')),
      pg_catalog.now()+pg_catalog.make_interval(secs=>ttl)) RETURNING id INTO request_id;
  END IF;
  RETURN request_id;
END
$fleet_worker_otp_request$;

CREATE OR REPLACE FUNCTION public.fleet_worker_otp_wait(p_request_id BIGINT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_otp_wait$
DECLARE principal public.fleet_worker_principals%ROWTYPE;
BEGIN
  SELECT * INTO principal FROM public.fleet_worker_principals
  WHERE role_name=session_user AND contract IN ('apply','linkedin');
  IF NOT FOUND AND session_user=current_user THEN
    SELECT worker_id INTO principal.worker_id FROM public.otp_request WHERE id=p_request_id;
  ELSIF NOT FOUND THEN RAISE EXCEPTION 'unmapped OTP worker' USING ERRCODE='insufficient_privilege'; END IF;
  UPDATE public.otp_request SET wait_started_at=COALESCE(wait_started_at,pg_catalog.now())
  WHERE id=p_request_id AND worker_id=principal.worker_id AND consumed_at IS NULL
    AND expires_at>pg_catalog.now();
  RETURN FOUND;
END
$fleet_worker_otp_wait$;

CREATE OR REPLACE FUNCTION public.fleet_worker_otp_consume(p_request_id BIGINT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_otp_consume$
DECLARE principal public.fleet_worker_principals%ROWTYPE; result JSONB;
BEGIN
  SELECT * INTO principal FROM public.fleet_worker_principals
  WHERE role_name=session_user AND contract IN ('apply','linkedin');
  IF NOT FOUND AND session_user=current_user THEN
    SELECT worker_id INTO principal.worker_id FROM public.otp_request WHERE id=p_request_id;
  ELSIF NOT FOUND THEN RAISE EXCEPTION 'unmapped OTP worker' USING ERRCODE='insufficient_privilege'; END IF;
  WITH picked AS (
    SELECT id,code,code_kind FROM public.otp_request
    WHERE id=p_request_id AND worker_id=principal.worker_id AND consumed_at IS NULL
      AND code IS NOT NULL AND expires_at>pg_catalog.now() FOR UPDATE
  )
  UPDATE public.otp_request o SET consumed_at=pg_catalog.now(),code=NULL
  FROM picked WHERE o.id=picked.id
  RETURNING pg_catalog.jsonb_build_object('value',picked.code,'kind',COALESCE(picked.code_kind,'code'))
  INTO result;
  RETURN result;
END
$fleet_worker_otp_consume$;

CREATE OR REPLACE FUNCTION public.fleet_controller_otp_pending(p_limit INTEGER DEFAULT 1000)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_controller_otp_pending$
BEGIN
  IF EXISTS(SELECT 1 FROM public.fleet_worker_principals WHERE role_name=session_user) THEN
    RAISE EXCEPTION 'worker principals cannot inspect pending OTP requests' USING ERRCODE='insufficient_privilege'; END IF;
  RETURN COALESCE((SELECT pg_catalog.jsonb_agg(pg_catalog.to_jsonb(x) ORDER BY x.requested_at,x.id)
    FROM (SELECT id,requested_at,sender_hint FROM public.otp_request
      WHERE code IS NULL AND consumed_at IS NULL AND expires_at>pg_catalog.now()
      ORDER BY requested_at,id LIMIT LEAST(GREATEST(COALESCE(p_limit,1000),1),1001)) x),'[]'::jsonb);
END
$fleet_controller_otp_pending$;

CREATE OR REPLACE FUNCTION public.fleet_controller_otp_used_messages(p_ids TEXT[])
RETURNS TEXT[] LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_controller_otp_used_messages$
BEGIN
  IF EXISTS(SELECT 1 FROM public.fleet_worker_principals WHERE role_name=session_user) THEN
    RAISE EXCEPTION 'worker principals cannot inspect OTP responder state' USING ERRCODE='insufficient_privilege'; END IF;
  RETURN COALESCE((SELECT pg_catalog.array_agg(matched_message_id) FROM public.otp_request
    WHERE matched_message_id=ANY(COALESCE(p_ids,ARRAY[]::text[]))),ARRAY[]::text[]);
END
$fleet_controller_otp_used_messages$;

CREATE OR REPLACE FUNCTION public.fleet_controller_otp_answer(
  p_request_id BIGINT,p_code TEXT,p_kind TEXT,p_email_ts TIMESTAMPTZ,
  p_message_id TEXT,p_answered_ttl INTEGER)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_controller_otp_answer$
BEGIN
  IF EXISTS(SELECT 1 FROM public.fleet_worker_principals WHERE role_name=session_user) THEN
    RAISE EXCEPTION 'worker principals cannot answer OTP requests' USING ERRCODE='insufficient_privilege'; END IF;
  IF NULLIF(p_code,'') IS NULL OR pg_catalog.length(p_code)>4096 OR p_kind NOT IN ('code','magic_link')
     OR NULLIF(p_message_id,'') IS NULL OR pg_catalog.length(p_message_id)>1000 THEN
    RAISE EXCEPTION 'invalid OTP answer evidence' USING ERRCODE='check_violation'; END IF;
  UPDATE public.otp_request SET code=p_code,code_kind=p_kind,matched_email_ts=p_email_ts,
    matched_message_id=p_message_id,answered_at=pg_catalog.now(),
    expires_at=GREATEST(expires_at,pg_catalog.now()+pg_catalog.make_interval(
      secs=>LEAST(GREATEST(COALESCE(p_answered_ttl,600),1),3600)))
  WHERE id=p_request_id AND code IS NULL AND consumed_at IS NULL AND expires_at>pg_catalog.now();
  RETURN FOUND;
END
$fleet_controller_otp_answer$;

CREATE OR REPLACE FUNCTION public.fleet_controller_otp_purge()
RETURNS INTEGER LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_controller_otp_purge$
DECLARE n INTEGER;
BEGIN
  IF EXISTS(SELECT 1 FROM public.fleet_worker_principals WHERE role_name=session_user) THEN
    RAISE EXCEPTION 'worker principals cannot purge OTP state' USING ERRCODE='insufficient_privilege'; END IF;
  UPDATE public.otp_request SET code=NULL WHERE code IS NOT NULL AND expires_at<=pg_catalog.now();
  GET DIAGNOSTICS n=ROW_COUNT; RETURN n;
END
$fleet_controller_otp_purge$;

REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_admission_snapshot() FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_schema_contract() FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_heartbeat(JSONB) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_runtime_state(TEXT) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_version_status(TEXT,TEXT) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_claim_liveness() FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_write_liveness(TEXT,TEXT,TEXT) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_agent_blocks() FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_record_agent_wall(TEXT,TIMESTAMPTZ) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_evaluate_agent_budget(JSONB,INTEGER,INTEGER) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_ack_command(BIGINT,TEXT) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_attempt_create(TEXT,TEXT,JSONB) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_attempt_transition(UUID,TEXT,TEXT,JSONB) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_lease_compute() FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_complete_compute(TEXT,TEXT,TEXT,JSONB,JSONB) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_lease_search() FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_complete_search(TEXT,JSONB,TEXT) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_controller_verify_submission(TEXT,TEXT,TEXT,TEXT) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_otp_request(TEXT,TEXT,TEXT,INTEGER) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_otp_wait(BIGINT) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_otp_consume(BIGINT) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_controller_otp_pending(INTEGER) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_controller_otp_used_messages(TEXT[]) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_controller_otp_answer(BIGINT,TEXT,TEXT,TIMESTAMPTZ,TEXT,INTEGER) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_controller_otp_purge() FROM PUBLIC;
