SET ROLE brain_schema_migrator;

GRANT USAGE ON SCHEMA public TO brain_artifact_authority_owner, brain_artifact_authority_writer;
GRANT CREATE ON SCHEMA public TO brain_artifact_authority_owner;
GRANT SELECT, INSERT ON TABLE public.brain_artifacts TO brain_artifact_authority_owner;
GRANT SELECT, INSERT ON TABLE public.brain_artifact_locations TO brain_artifact_authority_owner;
GRANT USAGE, SELECT ON SEQUENCE public.brain_artifact_locations_artifact_location_id_seq
TO brain_artifact_authority_owner;

CREATE TABLE public.brain_artifact_authority_requests (
    request_id UUID PRIMARY KEY,
    manifest_sha256 TEXT NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    purpose TEXT NOT NULL CHECK (purpose = 'brain-artifact-authority-registration-v1'),
    key_id TEXT NOT NULL CHECK (btrim(key_id) <> ''),
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL CHECK (expires_at > issued_at),
    destination_system_id TEXT NOT NULL CHECK (btrim(destination_system_id) <> ''),
    destination_database_name TEXT NOT NULL CHECK (btrim(destination_database_name) <> ''),
    artifact_count INTEGER NOT NULL CHECK (artifact_count > 0),
    receipt JSONB NOT NULL CHECK (jsonb_typeof(receipt) = 'object'),
    registered_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE public.brain_artifact_authority_registrations (
    request_id UUID NOT NULL REFERENCES public.brain_artifact_authority_requests(request_id),
    artifact_ordinal INTEGER NOT NULL CHECK (artifact_ordinal > 0),
    artifact_hash TEXT NOT NULL REFERENCES public.brain_artifacts(artifact_hash)
        CHECK (artifact_hash ~ '^[0-9a-f]{64}$'),
    byte_length BIGINT NOT NULL CHECK (byte_length >= 0),
    media_type TEXT NOT NULL CHECK (btrim(media_type) <> ''),
    backend TEXT NOT NULL CHECK (backend = 's3'),
    bucket TEXT NOT NULL CHECK (btrim(bucket) <> ''),
    object_key TEXT NOT NULL CHECK (btrim(object_key) <> ''),
    provider_version_id TEXT NOT NULL CHECK (btrim(provider_version_id) <> ''),
    provider_checksum TEXT NOT NULL CHECK (btrim(provider_checksum) <> ''),
    encryption_mode TEXT NOT NULL CHECK (encryption_mode IN ('provider_managed','customer_managed')),
    encryption_key_id TEXT NOT NULL CHECK (btrim(encryption_key_id) <> ''),
    policy_source_id TEXT,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (request_id, artifact_ordinal),
    UNIQUE (request_id, artifact_hash)
);

CREATE TRIGGER brain_artifact_authority_requests_append_only
BEFORE UPDATE OR DELETE ON public.brain_artifact_authority_requests
FOR EACH ROW EXECUTE FUNCTION public.brain_reject_mutation();
CREATE TRIGGER brain_artifact_authority_requests_no_truncate
BEFORE TRUNCATE ON public.brain_artifact_authority_requests
FOR EACH STATEMENT EXECUTE FUNCTION public.brain_reject_mutation();
CREATE TRIGGER brain_artifact_authority_registrations_append_only
BEFORE UPDATE OR DELETE ON public.brain_artifact_authority_registrations
FOR EACH ROW EXECUTE FUNCTION public.brain_reject_mutation();
CREATE TRIGGER brain_artifact_authority_registrations_no_truncate
BEFORE TRUNCATE ON public.brain_artifact_authority_registrations
FOR EACH STATEMENT EXECUTE FUNCTION public.brain_reject_mutation();

ALTER TABLE public.brain_artifact_authority_requests OWNER TO brain_artifact_authority_owner;
ALTER TABLE public.brain_artifact_authority_registrations OWNER TO brain_artifact_authority_owner;

CREATE FUNCTION public.brain_register_authoritative_artifact_manifest(
    request_id uuid, manifest_sha256 text, purpose text, key_id text,
    issued_at timestamptz, expires_at timestamptz,
    destination_system_id text, destination_database_name text, artifacts jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    existing public.brain_artifact_authority_requests%ROWTYPE;
    item RECORD;
    normalized_backend TEXT;
    expected_count INTEGER;
    actual_count INTEGER;
    database_system_id TEXT;
    result JSONB;
BEGIN
    IF request_id IS NULL OR manifest_sha256 !~ '^[0-9a-f]{64}$'
       OR purpose IS DISTINCT FROM 'brain-artifact-authority-registration-v1'
       OR key_id IS NULL OR btrim(key_id) = ''
       OR issued_at IS NULL OR expires_at IS NULL OR expires_at <= issued_at
       OR clock_timestamp() < issued_at OR clock_timestamp() >= expires_at THEN
        RAISE EXCEPTION 'invalid or expired artifact authority request' USING ERRCODE='22023';
    END IF;
    IF jsonb_typeof(artifacts) IS DISTINCT FROM 'array' OR jsonb_array_length(artifacts) = 0 THEN
        RAISE EXCEPTION 'artifacts must be a non-empty array' USING ERRCODE='22023';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended('brain-artifact-authority:' || request_id::text, 0));
    SELECT * INTO existing FROM public.brain_artifact_authority_requests r
    WHERE r.request_id = brain_register_authoritative_artifact_manifest.request_id;
    IF FOUND THEN
        IF existing.manifest_sha256 IS DISTINCT FROM manifest_sha256 THEN
            RAISE EXCEPTION 'request_id already registered with a different manifest digest'
                USING ERRCODE='23505';
        END IF;
        RETURN existing.receipt;
    END IF;

    SELECT system_identifier::text INTO database_system_id FROM pg_control_system();
    IF destination_system_id IS DISTINCT FROM database_system_id
       OR destination_database_name IS DISTINCT FROM current_database() THEN
        RAISE EXCEPTION 'artifact authority destination identity mismatch' USING ERRCODE='22023';
    END IF;

    expected_count := jsonb_array_length(artifacts);
    result := jsonb_build_object(
        'status','registered','request_id',request_id,'manifest_sha256',manifest_sha256,
        'purpose',purpose,'key_id',key_id,'issued_at',issued_at,'expires_at',expires_at,
        'destination_system_id',destination_system_id,
        'destination_database_name',destination_database_name,'artifact_count',expected_count
    );
    INSERT INTO public.brain_artifact_authority_requests
        (request_id,manifest_sha256,purpose,key_id,issued_at,expires_at,destination_system_id,
         destination_database_name,artifact_count,receipt)
    VALUES
        (request_id,manifest_sha256,purpose,key_id,issued_at,expires_at,destination_system_id,
         destination_database_name,expected_count,result);

    FOR item IN
        SELECT value, ordinality::integer AS ordinal
        FROM jsonb_array_elements(artifacts) WITH ORDINALITY
    LOOP
        IF jsonb_typeof(item.value) IS DISTINCT FROM 'object'
           OR (item.value->>'artifact_hash') !~ '^[0-9a-f]{64}$'
           OR (item.value->>'byte_length') !~ '^(0|[1-9][0-9]*)$'
           OR btrim(COALESCE(item.value->>'media_type','')) = ''
           OR btrim(COALESCE(item.value->>'bucket','')) = ''
           OR btrim(COALESCE(item.value->>'object_key','')) = ''
           OR btrim(COALESCE(item.value->>'provider_version_id','')) = ''
           OR btrim(COALESCE(item.value->>'provider_checksum','')) = ''
           OR COALESCE((item.value->>'storage_immutable')::boolean, false) IS NOT TRUE
           OR item.value->>'encryption_mode' NOT IN ('provider_managed','customer_managed')
           OR btrim(COALESCE(item.value->>'encryption_key_id','')) = '' THEN
            RAISE EXCEPTION 'invalid artifact at ordinal %', item.ordinal USING ERRCODE='22023';
        END IF;
        normalized_backend := CASE item.value->>'backend'
            WHEN 's3' THEN 's3' WHEN 'aws_s3' THEN 's3' ELSE NULL END;
        IF normalized_backend IS NULL THEN
            RAISE EXCEPTION 'unsupported artifact backend at ordinal %', item.ordinal USING ERRCODE='22023';
        END IF;

        INSERT INTO public.brain_artifacts
            (request_id,artifact_hash,media_type,byte_length,schema_version,provenance,location)
        VALUES
            (request_id::text || ':' || item.ordinal::text, item.value->>'artifact_hash',
             item.value->>'media_type', (item.value->>'byte_length')::bigint, 1,
             jsonb_build_object('authority_request_id',request_id,'policy_source_id',item.value->>'policy_source_id'),
             format('s3://%s/%s', item.value->>'bucket', item.value->>'object_key'))
        ON CONFLICT (artifact_hash) DO NOTHING;
        IF NOT EXISTS (
            SELECT 1 FROM public.brain_artifacts a
            WHERE a.artifact_hash=item.value->>'artifact_hash'
              AND a.media_type=item.value->>'media_type'
              AND a.byte_length=(item.value->>'byte_length')::bigint
        ) THEN
            RAISE EXCEPTION 'artifact metadata conflict at ordinal %', item.ordinal USING ERRCODE='23505';
        END IF;

        INSERT INTO public.brain_artifact_locations
            (artifact_hash,backend,bucket_or_container,object_key,provider_version_id,
             provider_checksum,storage_immutable,encryption_mode,encryption_key_id,durability,verified_at)
        VALUES
            (item.value->>'artifact_hash','s3',item.value->>'bucket',item.value->>'object_key',
             item.value->>'provider_version_id',item.value->>'provider_checksum',true,
             item.value->>'encryption_mode',item.value->>'encryption_key_id','verified',clock_timestamp())
        ON CONFLICT DO NOTHING;
        IF NOT EXISTS (
            SELECT 1 FROM public.brain_artifact_locations l
            WHERE l.artifact_hash=item.value->>'artifact_hash' AND l.backend='s3'
              AND l.bucket_or_container=item.value->>'bucket' AND l.object_key=item.value->>'object_key'
              AND l.provider_version_id=item.value->>'provider_version_id'
              AND l.provider_checksum=item.value->>'provider_checksum' AND l.storage_immutable
              AND l.encryption_mode=item.value->>'encryption_mode'
              AND l.encryption_key_id=item.value->>'encryption_key_id' AND l.durability='verified'
        ) THEN
            RAISE EXCEPTION 'artifact location conflict at ordinal %', item.ordinal USING ERRCODE='23505';
        END IF;

        INSERT INTO public.brain_artifact_authority_registrations
            (request_id,artifact_ordinal,artifact_hash,byte_length,media_type,backend,bucket,object_key,
             provider_version_id,provider_checksum,encryption_mode,encryption_key_id,policy_source_id)
        VALUES
            (request_id,item.ordinal,item.value->>'artifact_hash',(item.value->>'byte_length')::bigint,
             item.value->>'media_type','s3',item.value->>'bucket',item.value->>'object_key',
             item.value->>'provider_version_id',item.value->>'provider_checksum',
             item.value->>'encryption_mode',item.value->>'encryption_key_id',item.value->>'policy_source_id');
    END LOOP;

    SELECT count(*) INTO actual_count FROM public.brain_artifact_authority_registrations r
    WHERE r.request_id=brain_register_authoritative_artifact_manifest.request_id;
    IF actual_count <> expected_count THEN
        RAISE EXCEPTION 'artifact authority registration count mismatch' USING ERRCODE='23514';
    END IF;
    RETURN result;
END;
$$;

ALTER FUNCTION public.brain_register_authoritative_artifact_manifest(
    uuid,text,text,text,timestamptz,timestamptz,text,text,jsonb
) OWNER TO brain_artifact_authority_owner;

SET ROLE brain_schema_migrator;

CREATE OR REPLACE FUNCTION public.brain_artifact_is_authoritative(candidate_hash TEXT) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM public.brain_artifact_authority_registrations registration
        JOIN public.brain_artifact_locations location
          ON location.artifact_hash = registration.artifact_hash
        WHERE registration.artifact_hash = candidate_hash
          AND location.durability = 'verified'
          AND location.storage_immutable
          AND location.provider_version_id IS NOT NULL
          AND location.provider_checksum IS NOT NULL
          AND location.encryption_mode <> 'none'
          AND location.verified_at IS NOT NULL
    );
$$;

ALTER FUNCTION public.brain_artifact_is_authoritative(text)
OWNER TO brain_artifact_authority_owner;
REVOKE CREATE ON SCHEMA public FROM brain_artifact_authority_owner;
SET ROLE brain_artifact_authority_owner;
REVOKE ALL ON FUNCTION public.brain_artifact_is_authoritative(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.brain_artifact_is_authoritative(text) TO brain_schema_migrator;

SET ROLE brain_schema_migrator;

CREATE OR REPLACE FUNCTION public.brain_check_policy_lifecycle() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE required_roles CONSTANT TEXT[] := ARRAY[
    'qualification_model','preference_model','outcome_model','knowledge_graph','label_snapshot',
    'pairwise_snapshot','outcome_snapshot','config','metrics','replay'];
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.lifecycle <> 'draft' THEN RAISE EXCEPTION 'new policy must start in draft' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END IF;
    IF OLD.lifecycle <> 'draft' AND (NEW.lane IS DISTINCT FROM OLD.lane OR NEW.policy_metadata IS DISTINCT FROM OLD.policy_metadata OR NEW.gate_definition_version IS DISTINCT FROM OLD.gate_definition_version) THEN
        RAISE EXCEPTION 'policy lane, gate definition, and metadata are immutable after draft' USING ERRCODE='23514';
    END IF;
    IF NEW.lifecycle IS DISTINCT FROM OLD.lifecycle THEN
        PERFORM public.brain_require_controller();
        IF current_setting('applypilot.policy_transition',true) IS DISTINCT FROM 'locked' THEN RAISE EXCEPTION 'policy lifecycle changes require locked controller function' USING ERRCODE='42501'; END IF;
        IF NOT ((OLD.lifecycle='draft' AND NEW.lifecycle='validated') OR (OLD.lifecycle='validated' AND NEW.lifecycle='canary') OR (OLD.lifecycle='canary' AND NEW.lifecycle='active') OR (OLD.lifecycle='active' AND NEW.lifecycle='retired')) THEN RAISE EXCEPTION 'illegal policy lifecycle transition' USING ERRCODE='23514'; END IF;
        IF (SELECT count(*) FROM public.brain_policy_transition_receipts WHERE policy_version=NEW.policy_version AND lifecycle=NEW.lifecycle AND definition_version=NEW.gate_definition_version) <> (SELECT count(*) FROM public.brain_policy_gate_definitions WHERE definition_version=NEW.gate_definition_version AND lane=NEW.lane AND lifecycle=NEW.lifecycle AND mandatory) THEN RAISE EXCEPTION 'policy transition requires complete mandatory locked gate receipts' USING ERRCODE='23514'; END IF;
        IF NEW.lifecycle IN ('validated','canary','active') AND ((SELECT count(*) FROM public.brain_policy_artifacts WHERE policy_version=NEW.policy_version AND artifact_role=ANY(required_roles)) <> cardinality(required_roles) OR EXISTS (SELECT 1 FROM public.brain_policy_artifacts p WHERE p.policy_version=NEW.policy_version AND NOT public.brain_artifact_is_authoritative(p.artifact_hash)) OR NOT EXISTS (SELECT 1 FROM public.brain_policy_approvals WHERE policy_version=NEW.policy_version AND approval_type=NEW.lifecycle)) THEN
            RAISE EXCEPTION 'policy transition requires complete authoritative artifacts and approval' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

SET ROLE brain_artifact_authority_owner;

REVOKE ALL ON TABLE public.brain_artifact_authority_requests FROM PUBLIC;
REVOKE ALL ON TABLE public.brain_artifact_authority_registrations FROM PUBLIC;
REVOKE ALL ON FUNCTION public.brain_register_authoritative_artifact_manifest(uuid,text,text,text,timestamptz,timestamptz,text,text,jsonb) FROM PUBLIC;
GRANT SELECT ON TABLE public.brain_artifact_authority_requests, public.brain_artifact_authority_registrations TO brain_schema_verifier;
GRANT EXECUTE ON FUNCTION public.brain_register_authoritative_artifact_manifest(uuid,text,text,text,timestamptz,timestamptz,text,text,jsonb) TO brain_artifact_authority_writer;

SET ROLE brain_schema_migrator;
