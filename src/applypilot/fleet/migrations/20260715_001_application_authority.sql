-- Shared application authority across ATS and LinkedIn queue aliases.
-- This migration is intentionally independent of worker-reported queue status.

CREATE TABLE fleet_application_authority (
    canonical_application_id TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL,
    authority_epoch BIGINT NOT NULL DEFAULT 0 CHECK (authority_epoch >= 0),
    state TEXT NOT NULL DEFAULT 'claimable' CHECK (state IN (
        'claimable', 'leased', 'browser_interaction', 'ambiguous', 'terminal'
    )),
    owner_id TEXT,
    channel_scope TEXT,
    operation_id UUID,
    request_hash TEXT,
    lease_expires_at TIMESTAMPTZ,
    browser_interaction_at TIMESTAMPTZ,
    ambiguous_at TIMESTAMPTZ,
    terminal_at TIMESTAMPTZ,
    terminal_status TEXT,
    terminal_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fleet_application_authority_operation_ck CHECK (
        (operation_id IS NULL) = (request_hash IS NULL)
    ),
    CONSTRAINT fleet_application_authority_terminal_ck CHECK (
        (state = 'terminal') = (terminal_status IS NOT NULL AND terminal_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX fleet_application_authority_operation_uq
    ON fleet_application_authority(operation_id) WHERE operation_id IS NOT NULL;
CREATE UNIQUE INDEX fleet_application_authority_url_uq
    ON fleet_application_authority(lower(canonical_url));

CREATE TABLE fleet_application_alias (
    queue_name TEXT NOT NULL CHECK (queue_name IN ('apply_queue', 'linkedin_queue')),
    url TEXT NOT NULL,
    canonical_application_id TEXT NOT NULL REFERENCES fleet_application_authority(canonical_application_id),
    channel_scope TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (queue_name, url)
);

CREATE OR REPLACE FUNCTION fleet_worker_authorize_lease(
    p_canonical_application_id TEXT,
    p_canonical_url TEXT,
    p_owner_id TEXT,
    p_channel_scope TEXT,
    p_operation_id UUID,
    p_request_hash TEXT,
    p_ttl_seconds INTEGER
) RETURNS TABLE(authority_epoch BIGINT, operation_id UUID)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    current_row fleet_application_authority%ROWTYPE;
BEGIN
    IF p_ttl_seconds IS NULL OR p_ttl_seconds <= 0 THEN
        RAISE EXCEPTION 'lease ttl must be positive';
    END IF;
    INSERT INTO fleet_application_authority(
        canonical_application_id, canonical_url, owner_id, channel_scope,
        operation_id, request_hash, state, lease_expires_at
    ) VALUES (
        p_canonical_application_id, p_canonical_url, p_owner_id, p_channel_scope,
        p_operation_id, p_request_hash, 'leased', now() + make_interval(secs => p_ttl_seconds)
    ) ON CONFLICT (canonical_application_id) DO NOTHING;

    SELECT * INTO current_row
      FROM fleet_application_authority
     WHERE canonical_application_id = p_canonical_application_id
     FOR UPDATE;

    IF current_row.state IN ('browser_interaction', 'ambiguous', 'terminal') THEN
        RAISE EXCEPTION 'application authority is not claimable: %', current_row.state;
    END IF;
    IF current_row.operation_id IS NOT NULL AND
       (current_row.operation_id <> p_operation_id OR current_row.request_hash <> p_request_hash) THEN
        RAISE EXCEPTION 'operation/request binding conflict';
    END IF;
    UPDATE fleet_application_authority
       SET authority_epoch = fleet_application_authority.authority_epoch + 1,
           owner_id = p_owner_id,
           channel_scope = p_channel_scope,
           operation_id = p_operation_id,
           request_hash = p_request_hash,
           state = 'leased',
           lease_expires_at = now() + make_interval(secs => p_ttl_seconds),
           updated_at = now()
     WHERE canonical_application_id = p_canonical_application_id
     RETURNING fleet_application_authority.authority_epoch, fleet_application_authority.operation_id
      INTO authority_epoch, operation_id;
    RETURN NEXT;
END $$;

CREATE OR REPLACE FUNCTION fleet_worker_mark_browser_interaction(
    p_canonical_application_id TEXT, p_owner_id TEXT, p_authority_epoch BIGINT
) RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    UPDATE fleet_application_authority
       SET state = 'browser_interaction', browser_interaction_at = COALESCE(browser_interaction_at, now()), updated_at = now()
     WHERE canonical_application_id = p_canonical_application_id
       AND owner_id = p_owner_id AND authority_epoch = p_authority_epoch
       AND state = 'leased';
    RETURN FOUND;
END $$;

CREATE OR REPLACE FUNCTION fleet_worker_terminalize(
    p_canonical_application_id TEXT, p_owner_id TEXT, p_authority_epoch BIGINT,
    p_terminal_status TEXT, p_evidence JSONB
) RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    IF p_terminal_status IS NULL OR p_evidence IS NULL OR p_evidence = '{}'::jsonb THEN
        RAISE EXCEPTION 'terminalization requires status and evidence';
    END IF;
    UPDATE fleet_application_authority
       SET state = 'terminal', terminal_status = p_terminal_status, terminal_evidence = p_evidence,
           terminal_at = now(), lease_expires_at = NULL, updated_at = now()
     WHERE canonical_application_id = p_canonical_application_id
       AND owner_id = p_owner_id AND authority_epoch = p_authority_epoch
       AND state IN ('leased', 'browser_interaction', 'ambiguous');
    RETURN FOUND;
END $$;

CREATE OR REPLACE FUNCTION fleet_worker_requeue(
    p_canonical_application_id TEXT, p_owner_id TEXT, p_authority_epoch BIGINT
) RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    UPDATE fleet_application_authority
       SET state = 'claimable', owner_id = NULL, lease_expires_at = NULL, updated_at = now()
     WHERE canonical_application_id = p_canonical_application_id
       AND owner_id = p_owner_id AND authority_epoch = p_authority_epoch
       AND state = 'leased' AND browser_interaction_at IS NULL;
    RETURN FOUND;
END $$;

CREATE OR REPLACE FUNCTION fleet_worker_expire_authority()
RETURNS INTEGER LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE n INTEGER;
BEGIN
    UPDATE fleet_application_authority
       SET state = CASE WHEN browser_interaction_at IS NULL THEN 'claimable' ELSE 'ambiguous' END,
           ambiguous_at = CASE WHEN browser_interaction_at IS NULL THEN ambiguous_at ELSE COALESCE(ambiguous_at, now()) END,
           owner_id = NULL, lease_expires_at = NULL, updated_at = now()
     WHERE state = 'leased' AND lease_expires_at < now();
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

REVOKE ALL ON fleet_application_authority, fleet_application_alias FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fleet_worker') THEN
        GRANT SELECT, INSERT, UPDATE ON fleet_application_authority, fleet_application_alias TO fleet_worker;
        GRANT EXECUTE ON FUNCTION fleet_worker_authorize_lease(TEXT,TEXT,TEXT,TEXT,UUID,TEXT,INTEGER),
            fleet_worker_mark_browser_interaction(TEXT,TEXT,BIGINT),
            fleet_worker_terminalize(TEXT,TEXT,BIGINT,TEXT,JSONB),
            fleet_worker_requeue(TEXT,TEXT,BIGINT), fleet_worker_expire_authority() TO fleet_worker;
    END IF;
END $$;
