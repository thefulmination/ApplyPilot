-- ===========================================================================
-- Cloud apply-fleet schema (Railway Postgres) -- queue-offload design.
-- See docs/superpowers/specs/2026-06-24-cloud-apply-fleet-design.md (S3a, S5, S6).
--
-- Idempotent: safe to run on every worker/home startup (CREATE ... IF NOT EXISTS,
-- ENUM guarded by a duplicate_object catch). This is the ONLY state in the cloud --
-- a thin work queue + result mailbox. Home SQLite stays authoritative.
-- ===========================================================================

-- apply_queue_status: referenced by name in the lease/reclaim SQL, so an ENUM
-- (not a CHECK) -- but CREATE TYPE has no IF NOT EXISTS, so guard it.
DO $$ BEGIN
    CREATE TYPE apply_queue_status AS ENUM (
        'queued',             -- pushed, eligible to lease
        'leased',             -- a worker holds it (lease_expires_at in the future)
        'applied',            -- submit confirmed
        'failed',             -- terminal non-submit (expired/captcha/page_error/...)
        'blocked',            -- site/cloudflare/auth wall the offsite agent can't pass
        'crash_unconfirmed'   -- worker died mid-job, possibly post-submit: NEVER re-leased
    );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS apply_queue (
    -- ---- identity / job columns (pushed from home) ------------------------
    url                     TEXT        PRIMARY KEY,          -- = jobs.url (cross-system key + idempotency anchor)
    company                 TEXT,
    title                   TEXT,
    application_url         TEXT        NOT NULL,             -- offsite ATS form target
    score                   REAL        NOT NULL,             -- COALESCE(audit_score, fit_score); REAL, not INT (tie-break fidelity)
    apply_domain            TEXT,                             -- effective apply host (politeness key)

    -- ---- queue / lease state ---------------------------------------------
    status                  apply_queue_status NOT NULL DEFAULT 'queued',
    lease_owner             TEXT,
    lease_expires_at        TIMESTAMPTZ,
    last_attempted_at       TIMESTAMPTZ,                      -- set at lease time (politeness)
    attempts                INTEGER     NOT NULL DEFAULT 0,

    -- ---- result columns (written by the fleet) ---------------------------
    apply_status            TEXT,                             -- raw agent outcome: applied / failed:<reason> / expired / captcha ...
    apply_error             TEXT,
    verification_confidence TEXT,                             -- pass-through (NULL in live DB today)
    agent_model             TEXT,                             -- provider/model that ran this job (the Sonnet-vs-DeepSeek A/B)
    est_cost_usd            NUMERIC(10,4),                    -- apply-agent total_cost_usd (drives the cap)
    applied_at              TIMESTAMPTZ,
    worker_id               TEXT,
    machine_owner           TEXT,
    apply_duration_ms       INTEGER,

    -- ---- bookkeeping ------------------------------------------------------
    pushed_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    synced_to_home_at       TIMESTAMPTZ                       -- set by PULL; NULL = not yet ingested home
);

-- Lease query: WHERE status='queued' ORDER BY score DESC LIMIT 1.
CREATE INDEX IF NOT EXISTS idx_apply_queue_lease
    ON apply_queue (score DESC)
    WHERE status = 'queued';

-- Reclaim scan: leased rows whose lease has expired.
CREATE INDEX IF NOT EXISTS idx_apply_queue_reclaim
    ON apply_queue (lease_expires_at)
    WHERE status = 'leased';

-- PULL scan: terminal rows not yet ingested back into home SQLite.
CREATE INDEX IF NOT EXISTS idx_apply_queue_unsynced
    ON apply_queue (updated_at)
    WHERE status IN ('applied','failed','blocked','crash_unconfirmed')
      AND synced_to_home_at IS NULL;

-- Politeness scan: recently-touched domains.
CREATE INDEX IF NOT EXISTS idx_apply_queue_host_recent
    ON apply_queue (apply_domain, last_attempted_at);

-- Single-row global control: spend cap + kill switch. id=1 enforced.
CREATE TABLE IF NOT EXISTS fleet_config (
    id              INTEGER       PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    spend_cap_usd   NUMERIC(10,2) NOT NULL DEFAULT 0,        -- 0 = no cap; halt when SUM(est_cost_usd) >= this
    paused          BOOLEAN       NOT NULL DEFAULT FALSE,    -- global kill switch
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

INSERT INTO fleet_config (id, spend_cap_usd, paused)
VALUES (1, 0, FALSE)
ON CONFLICT (id) DO NOTHING;

-- Worker assets (profile.json + resume.pdf) shipped THROUGH Postgres, so the fleet needs no
-- volume/secret juggling for PII: the home box uploads them once, each worker hydrates them to
-- disk on startup. BYTEA holds the binary resume PDF.
CREATE TABLE IF NOT EXISTS fleet_assets (
    name       TEXT        PRIMARY KEY,
    data       BYTEA       NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Durable terminal evidence is also part of the base queue schema so the
-- watchdog can record lease reclaims before the v3 fleet layer is bootstrapped.
CREATE TABLE IF NOT EXISTS apply_result_events (
    id                      BIGSERIAL PRIMARY KEY,
    queue_name              TEXT NOT NULL DEFAULT 'apply_queue',
    url                     TEXT NOT NULL,
    worker_id               TEXT,
    status                  TEXT,
    apply_status            TEXT,
    apply_error             TEXT,
    target_host             TEXT,
    home_ip                 TEXT,
    agent                   TEXT,
    agent_model             TEXT,
    est_cost_usd            REAL,
    apply_duration_ms       INTEGER,
    application_tool_calls  INTEGER,
    job_log_path            TEXT,
    transcript_digest       TEXT,
    final_result_source     TEXT,
    result_metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_line             TEXT,
    source                  TEXT NOT NULL DEFAULT 'worker',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_apply_result_events_url_created
    ON apply_result_events (queue_name, url, created_at DESC);
