-- Additive V4 authority contract. Earlier migrations are immutable.

CREATE TABLE public.brain_authority_scope_state (
    authority_scope_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id TEXT NOT NULL CHECK (btrim(owner_id) <> ''),
    campaign_id TEXT NOT NULL CHECK (btrim(campaign_id) <> ''),
    recommendation_lane TEXT NOT NULL CHECK (recommendation_lane IN (
        'core_fit', 'strategic_stretch', 'qualified_fallback', 'review', 'reject_hold'
    )),
    execution_channel TEXT NOT NULL CHECK (execution_channel IN ('ats', 'linkedin')),
    execution_scope TEXT NOT NULL CHECK (
        btrim(execution_scope) <> '' AND lower(btrim(execution_scope)) <> 'global'
    ),
    state TEXT NOT NULL DEFAULT 'active' CHECK (state = 'active'),
    authority_epoch BIGINT NOT NULL CHECK (authority_epoch > 0),
    database_incarnation_id UUID NOT NULL,
    migration_run_id BIGINT NOT NULL,
    source_namespace TEXT NOT NULL,
    parity_run_id BIGINT NOT NULL,
    definition_version INTEGER NOT NULL,
    report_artifact_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, campaign_id, recommendation_lane, execution_channel, execution_scope),
    CONSTRAINT brain_authority_scope_state_parity_fk FOREIGN KEY
        (migration_run_id, source_namespace, parity_run_id, definition_version, report_artifact_hash)
        REFERENCES public.brain_parity_runs
        (migration_run_id, source_namespace, parity_run_id, definition_version, report_artifact_hash)
);

CREATE TABLE public.brain_authority_transition_events (
    authority_transition_event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    authority_scope_id BIGINT NOT NULL REFERENCES public.brain_authority_scope_state(authority_scope_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('granted', 'revoked', 'candidate_published')),
    authority_epoch BIGINT NOT NULL CHECK (authority_epoch > 0),
    database_incarnation_id UUID NOT NULL,
    actor_id TEXT NOT NULL CHECK (btrim(actor_id) <> ''),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (authority_scope_id, event_type, authority_epoch, database_incarnation_id)
);

CREATE TABLE public.brain_graph_approval_receipts (
    graph_approval_receipt_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    authority_scope_id BIGINT NOT NULL REFERENCES public.brain_authority_scope_state(authority_scope_id),
    authority_epoch BIGINT NOT NULL CHECK (authority_epoch > 0),
    database_incarnation_id UUID NOT NULL,
    graph_snapshot_id TEXT NOT NULL CHECK (btrim(graph_snapshot_id) <> ''),
    approval_state TEXT NOT NULL CHECK (approval_state IN ('approved', 'denied')),
    approval_artifact_hash TEXT NOT NULL REFERENCES public.brain_artifacts(artifact_hash),
    predecessor_deny_receipt_hash TEXT REFERENCES public.brain_artifacts(artifact_hash),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (authority_scope_id, authority_epoch, database_incarnation_id, graph_snapshot_id)
);

CREATE TABLE public.brain_v4_candidate_decisions (
    candidate_decision_id TEXT PRIMARY KEY CHECK (candidate_decision_id ~ '^decision-[a-z0-9_-]+$'),
    authority_scope_id BIGINT NOT NULL REFERENCES public.brain_authority_scope_state(authority_scope_id),
    semantic_content_hash TEXT NOT NULL CHECK (semantic_content_hash ~ '^[0-9a-f]{64}$'),
    candidate_artifact_hash TEXT NOT NULL REFERENCES public.brain_artifacts(artifact_hash),
    graph_approval_receipt_id BIGINT NOT NULL REFERENCES public.brain_graph_approval_receipts(graph_approval_receipt_id),
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (authority_scope_id, semantic_content_hash)
);

CREATE TABLE public.brain_v4_decision_envelopes (
    envelope_id TEXT PRIMARY KEY CHECK (btrim(envelope_id) <> ''),
    candidate_decision_id TEXT NOT NULL UNIQUE REFERENCES public.brain_v4_candidate_decisions(candidate_decision_id),
    envelope_artifact_hash TEXT NOT NULL REFERENCES public.brain_artifacts(artifact_hash),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE public.brain_graph_approval_consumptions (
    graph_approval_consumption_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    graph_approval_receipt_id BIGINT NOT NULL UNIQUE
        REFERENCES public.brain_graph_approval_receipts(graph_approval_receipt_id),
    candidate_decision_id TEXT NOT NULL UNIQUE REFERENCES public.brain_v4_candidate_decisions(candidate_decision_id),
    authority_scope_id BIGINT NOT NULL REFERENCES public.brain_authority_scope_state(authority_scope_id),
    consumed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE public.brain_immutable_artifact_references (
    artifact_reference_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    artifact_hash TEXT NOT NULL REFERENCES public.brain_artifacts(artifact_hash),
    reference_type TEXT NOT NULL CHECK (reference_type IN (
        'candidate_payload', 'decision_envelope', 'graph_approval_receipt', 'predecessor_deny_receipt'
    )),
    subject_id TEXT NOT NULL CHECK (btrim(subject_id) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (artifact_hash, reference_type, subject_id)
);

CREATE FUNCTION public.brain_publish_v4_candidate(
    requested_owner_id TEXT,
    requested_campaign_id TEXT,
    requested_recommendation_lane TEXT,
    requested_execution_channel TEXT,
    requested_execution_scope TEXT,
    requested_authority_epoch BIGINT,
    requested_database_incarnation_id UUID,
    requested_candidate_decision_id TEXT,
    requested_semantic_content_hash TEXT,
    requested_candidate_artifact_hash TEXT,
    requested_envelope_id TEXT,
    requested_envelope_artifact_hash TEXT,
    requested_graph_approval_receipt_id BIGINT
) RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE scope_row public.brain_authority_scope_state%ROWTYPE;
        approval_row public.brain_graph_approval_receipts%ROWTYPE;
BEGIN
    IF current_user <> 'brain_schema_migrator' THEN
        RAISE EXCEPTION 'candidate publish requires the schema-owned procedure' USING ERRCODE='42501';
    END IF;
    SELECT * INTO scope_row FROM public.brain_authority_scope_state
    WHERE owner_id=requested_owner_id AND campaign_id=requested_campaign_id
      AND recommendation_lane=requested_recommendation_lane AND execution_channel=requested_execution_channel
      AND execution_scope=requested_execution_scope FOR UPDATE;
    IF NOT FOUND OR scope_row.state <> 'active' THEN
        RAISE EXCEPTION 'active authority scope is required' USING ERRCODE='55000';
    END IF;
    IF scope_row.authority_epoch <> requested_authority_epoch THEN
        RAISE EXCEPTION 'stale authority epoch' USING ERRCODE='55000';
    END IF;
    IF scope_row.database_incarnation_id <> requested_database_incarnation_id THEN
        RAISE EXCEPTION 'wrong database incarnation' USING ERRCODE='55000';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.brain_authority_transition_events event
        WHERE event.authority_scope_id=scope_row.authority_scope_id AND event.event_type='granted'
          AND event.authority_epoch=scope_row.authority_epoch
          AND event.database_incarnation_id=scope_row.database_incarnation_id
    ) THEN
        RAISE EXCEPTION 'authority grant transition is required' USING ERRCODE='55000';
    END IF;
    SELECT * INTO approval_row FROM public.brain_graph_approval_receipts
    WHERE graph_approval_receipt_id=requested_graph_approval_receipt_id
      AND authority_scope_id=scope_row.authority_scope_id FOR UPDATE;
    IF NOT FOUND OR approval_row.approval_state <> 'approved'
       OR approval_row.authority_epoch <> scope_row.authority_epoch
       OR approval_row.database_incarnation_id <> scope_row.database_incarnation_id THEN
        RAISE EXCEPTION 'matching approved graph receipt is required' USING ERRCODE='55000';
    END IF;
    IF approval_row.predecessor_deny_receipt_hash IS NULL OR NOT EXISTS (
        SELECT 1 FROM public.brain_immutable_artifact_references reference
        WHERE reference.artifact_hash=approval_row.predecessor_deny_receipt_hash
          AND reference.reference_type='predecessor_deny_receipt'
          AND reference.subject_id=approval_row.graph_snapshot_id
    ) THEN
        RAISE EXCEPTION 'predecessor deny receipt is required' USING ERRCODE='55000';
    END IF;
    INSERT INTO public.brain_v4_candidate_decisions(
        candidate_decision_id,authority_scope_id,semantic_content_hash,candidate_artifact_hash,graph_approval_receipt_id
    ) VALUES (
        requested_candidate_decision_id,scope_row.authority_scope_id,requested_semantic_content_hash,
        requested_candidate_artifact_hash,approval_row.graph_approval_receipt_id
    );
    INSERT INTO public.brain_v4_decision_envelopes(envelope_id,candidate_decision_id,envelope_artifact_hash)
    VALUES (requested_envelope_id,requested_candidate_decision_id,requested_envelope_artifact_hash);
    INSERT INTO public.brain_immutable_artifact_references(artifact_hash,reference_type,subject_id)
    VALUES (requested_candidate_artifact_hash,'candidate_payload',requested_candidate_decision_id),
           (requested_envelope_artifact_hash,'decision_envelope',requested_envelope_id),
           (approval_row.approval_artifact_hash,'graph_approval_receipt',approval_row.graph_snapshot_id);
    INSERT INTO public.brain_graph_approval_consumptions(
        graph_approval_receipt_id,candidate_decision_id,authority_scope_id
    ) VALUES (approval_row.graph_approval_receipt_id,requested_candidate_decision_id,scope_row.authority_scope_id);
    INSERT INTO public.brain_authority_transition_events(
        authority_scope_id,event_type,authority_epoch,database_incarnation_id,actor_id
    ) VALUES (
        scope_row.authority_scope_id,'candidate_published',scope_row.authority_epoch,
        scope_row.database_incarnation_id,session_user
    );
    RETURN requested_candidate_decision_id;
END;
$$;

DO $$
DECLARE relation_name TEXT;
BEGIN
    FOREACH relation_name IN ARRAY ARRAY[
        'brain_authority_scope_state', 'brain_authority_transition_events', 'brain_graph_approval_receipts',
        'brain_v4_candidate_decisions', 'brain_v4_decision_envelopes', 'brain_graph_approval_consumptions',
        'brain_immutable_artifact_references'
    ] LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON public.%I '
            || 'FOR EACH ROW EXECUTE FUNCTION public.brain_reject_mutation()', relation_name || '_immutable', relation_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.brain_authority_scope_state, public.brain_authority_transition_events,
    public.brain_graph_approval_receipts, public.brain_v4_candidate_decisions, public.brain_v4_decision_envelopes,
    public.brain_graph_approval_consumptions, public.brain_immutable_artifact_references
FROM PUBLIC, brain_candidate_reader, brain_candidate_writer;
REVOKE ALL PRIVILEGES ON FUNCTION public.brain_publish_v4_candidate(
    TEXT,TEXT,TEXT,TEXT,TEXT,BIGINT,UUID,TEXT,TEXT,TEXT,TEXT,TEXT,BIGINT
) FROM PUBLIC, brain_candidate_reader, brain_candidate_writer;
REVOKE CREATE ON SCHEMA public FROM brain_candidate_reader, brain_candidate_writer;
GRANT USAGE ON SCHEMA public TO brain_candidate_reader, brain_candidate_writer;
GRANT SELECT ON TABLE public.brain_authority_scope_state, public.brain_v4_candidate_decisions,
    public.brain_v4_decision_envelopes, public.brain_immutable_artifact_references TO brain_candidate_reader;
GRANT SELECT ON TABLE public.brain_authority_scope_state, public.brain_authority_transition_events,
    public.brain_graph_approval_receipts, public.brain_v4_candidate_decisions, public.brain_v4_decision_envelopes,
    public.brain_graph_approval_consumptions, public.brain_immutable_artifact_references TO brain_schema_verifier;
GRANT EXECUTE ON FUNCTION public.brain_publish_v4_candidate(
    TEXT,TEXT,TEXT,TEXT,TEXT,BIGINT,UUID,TEXT,TEXT,TEXT,TEXT,TEXT,BIGINT
) TO brain_candidate_writer;
