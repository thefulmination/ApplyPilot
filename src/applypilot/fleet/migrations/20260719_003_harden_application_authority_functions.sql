-- Harden the legacy application-authority worker boundary installed by 001.
-- These exact overloads predate the queue-worker functions with the same names.

ALTER FUNCTION public.fleet_worker_authorize_lease(TEXT, TEXT, TEXT, TEXT, UUID, TEXT, INTEGER)
    SET search_path = pg_catalog, public;
ALTER FUNCTION public.fleet_worker_mark_browser_interaction(TEXT, TEXT, BIGINT)
    SET search_path = pg_catalog, public;
ALTER FUNCTION public.fleet_worker_terminalize(TEXT, TEXT, BIGINT, TEXT, JSONB)
    SET search_path = pg_catalog, public;
ALTER FUNCTION public.fleet_worker_requeue(TEXT, TEXT, BIGINT)
    SET search_path = pg_catalog, public;
ALTER FUNCTION public.fleet_worker_expire_authority()
    SET search_path = pg_catalog, public;

REVOKE ALL PRIVILEGES ON FUNCTION
    public.fleet_worker_authorize_lease(TEXT, TEXT, TEXT, TEXT, UUID, TEXT, INTEGER),
    public.fleet_worker_mark_browser_interaction(TEXT, TEXT, BIGINT),
    public.fleet_worker_terminalize(TEXT, TEXT, BIGINT, TEXT, JSONB),
    public.fleet_worker_requeue(TEXT, TEXT, BIGINT),
    public.fleet_worker_expire_authority()
FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'fleet_worker') THEN
        GRANT EXECUTE ON FUNCTION
            public.fleet_worker_authorize_lease(TEXT, TEXT, TEXT, TEXT, UUID, TEXT, INTEGER),
            public.fleet_worker_mark_browser_interaction(TEXT, TEXT, BIGINT),
            public.fleet_worker_terminalize(TEXT, TEXT, BIGINT, TEXT, JSONB),
            public.fleet_worker_requeue(TEXT, TEXT, BIGINT),
            public.fleet_worker_expire_authority()
        TO fleet_worker;
    END IF;
END
$$;
