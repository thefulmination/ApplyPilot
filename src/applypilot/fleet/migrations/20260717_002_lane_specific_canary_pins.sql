ALTER TABLE public.fleet_config
    ADD COLUMN IF NOT EXISTS ats_canary_worker_id TEXT,
    ADD COLUMN IF NOT EXISTS ats_canary_version TEXT,
    ADD COLUMN IF NOT EXISTS linkedin_canary_worker_id TEXT,
    ADD COLUMN IF NOT EXISTS linkedin_canary_version TEXT;

CREATE OR REPLACE FUNCTION public.fleet_worker_expected_version(
    p_worker TEXT,
    p_contract TEXT
) RETURNS TEXT
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $fleet_worker_expected_version$
    SELECT CASE
        WHEN p_contract = 'apply'
             AND c.ats_canary_worker_id = p_worker
             AND c.ats_canary_version IS NOT NULL
            THEN c.ats_canary_version
        WHEN p_contract = 'linkedin'
             AND c.linkedin_canary_worker_id = p_worker
             AND c.linkedin_canary_version IS NOT NULL
            THEN c.linkedin_canary_version
        WHEN c.canary_worker_id = p_worker AND c.canary_version IS NOT NULL
            THEN c.canary_version
        ELSE c.pinned_worker_version
    END
    FROM public.fleet_config c
    WHERE c.id = 1
$fleet_worker_expected_version$;

REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_expected_version(TEXT, TEXT) FROM PUBLIC;

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
    expected_version := public.fleet_worker_expected_version(p_worker, 'apply');
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
    expected_version := public.fleet_worker_expected_version(p_worker, 'linkedin');
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
        AND h.sw_version=public.fleet_worker_expected_version(w.worker_id,$1)
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
        WHEN h.sw_version IS DISTINCT FROM public.fleet_worker_expected_version(w.worker_id,$1)
          THEN 'version_mismatch'
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

CREATE OR REPLACE FUNCTION public.fleet_worker_version_status(p_worker TEXT,p_reported TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog, public
AS $fleet_worker_version_status$
DECLARE principal public.fleet_worker_principals%ROWTYPE; expected TEXT; actual TEXT; worker_contract TEXT;
BEGIN
  SELECT * INTO principal FROM public.fleet_worker_principals WHERE role_name=session_user;
  IF FOUND THEN
    p_worker:=principal.worker_id;
    worker_contract:=principal.contract;
    SELECT h.sw_version INTO actual FROM public.worker_heartbeat h WHERE h.worker_id=p_worker;
  ELSIF session_user=current_user THEN
    actual:=p_reported;
    SELECT CASE h.role
      WHEN 'apply' THEN 'apply'
      WHEN 'linkedin' THEN 'linkedin'
      WHEN 'compute' THEN 'compute'
      WHEN 'discovery' THEN 'discovery'
      WHEN 'discover' THEN 'discovery'
      ELSE NULL END
    INTO worker_contract FROM public.worker_heartbeat h WHERE h.worker_id=p_worker;
    IF worker_contract IS NULL THEN
      SELECT CASE WHEN pg_catalog.count(*)=1 THEN pg_catalog.min(p.contract) ELSE NULL END
      INTO worker_contract FROM public.fleet_worker_principals p WHERE p.worker_id=p_worker;
    END IF;
  ELSE RAISE EXCEPTION 'unmapped worker principal' USING ERRCODE='insufficient_privilege'; END IF;
  expected:=public.fleet_worker_expected_version(p_worker,worker_contract);
  RETURN pg_catalog.jsonb_build_object('expected_version',expected,'sw_version',actual,
    'matches',expected IS NULL OR actual=expected);
END
$fleet_worker_version_status$;

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
      AND h.sw_version IS NOT DISTINCT FROM public.fleet_worker_expected_version(w.worker_id,'compute')
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
      AND h.sw_version IS NOT DISTINCT FROM public.fleet_worker_expected_version(w.worker_id,'discovery')
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

DO $lane_canary_helper_acl$
DECLARE
    api_owner NAME;
BEGIN
    FOR api_owner IN
        SELECT DISTINCT owner_role.rolname
        FROM pg_catalog.pg_proc function
        JOIN pg_catalog.pg_namespace namespace ON namespace.oid = function.pronamespace
        JOIN pg_catalog.pg_roles owner_role ON owner_role.oid = function.proowner
        WHERE namespace.nspname = 'public'
          AND function.oid = ANY(ARRAY[
              'public.fleet_worker_lease_ats(text,text,integer,text,integer)'::regprocedure,
              'public.fleet_worker_lease_linkedin(text,text,text,integer,text)'::regprocedure,
              'public.fleet_worker_admission_snapshot()'::regprocedure,
              'public.fleet_worker_version_status(text,text)'::regprocedure,
              'public.fleet_worker_lease_compute()'::regprocedure,
              'public.fleet_worker_lease_search()'::regprocedure
          ])
    LOOP
        IF EXISTS (
            SELECT 1 FROM public.fleet_worker_principals principal
            WHERE principal.role_name = api_owner
        ) THEN
            RAISE EXCEPTION 'worker API owner % is mapped as a worker principal', api_owner
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        EXECUTE pg_catalog.format(
            'GRANT EXECUTE ON FUNCTION public.fleet_worker_expected_version(TEXT,TEXT) TO %I',
            api_owner
        );
    END LOOP;
END
$lane_canary_helper_acl$;

REVOKE ALL PRIVILEGES ON FUNCTION
    public.fleet_worker_lease_ats(TEXT,TEXT,INTEGER,TEXT,INTEGER),
    public.fleet_worker_lease_linkedin(TEXT,TEXT,TEXT,INTEGER,TEXT),
    public.fleet_worker_admission_snapshot(),
    public.fleet_worker_version_status(TEXT,TEXT),
    public.fleet_worker_lease_compute(),
    public.fleet_worker_lease_search()
FROM PUBLIC;
