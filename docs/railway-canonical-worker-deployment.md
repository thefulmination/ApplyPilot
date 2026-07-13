# Railway Canonical Worker Deployment

The Railway worker runs the canonical v3 `applypilot-fleet-apply` executable. The
retired `applypilot.apply.container_worker` must never be used.

## Required service configuration

Set these Railway variables before deploying:

- `DATABASE_URL`: reference the intended Postgres service. Never paste the home
  fleet DSN into a public variable.
- `DEEPSEEK_API_KEY`: sealed service variable used by the local LiteLLM proxy.
- `APPLYPILOT_WORKER_ID`: unique stable worker identity. Do not reuse one value
  across replicas.
- `APPLYPILOT_RELEASE_VERSION`: exact value pinned in
  `fleet_config.pinned_worker_version`, formatted as
  `0.3.0+git.tree.<7-lowercase-hex>`.

Optional variables:

- `FLEET_MACHINE_OWNER` and `APPLYPILOT_FLEET_LABEL`, both defaulting to
  `railway`. When either is set, set both to the same value.
- `FLEET_HOME_IP`: egress identity recorded in telemetry; defaults to `railway`.
- `APPLYPILOT_APPLY_AGENT`: defaults to `claude`.
- `APPLYPILOT_APPLY_MODEL`: defaults to `deepseek-chat`.
- `APPLYPILOT_FALLBACK_AGENT`: optional comma-separated fallback chain.
- `APPLYPILOT_CHROME_SLOT`: required when multiple workers share a filesystem or
  browser namespace.

Attach a persistent volume at `/data/applypilot` containing non-empty
`profile.json` and `resume.pdf`. The entrypoint refuses to start without them.

## Release sequence

1. Keep global and ATS lane pauses enabled, canary capacity at zero, and spend
   caps at zero.
2. Build the image from the reviewed release commit.
3. Calculate that commit's tree identity with
   `python -c "from applypilot.fleet.software_version import current_sw_version; print(current_sw_version())"`.
4. Set `APPLYPILOT_RELEASE_VERSION` to that exact value and pin the same value in
   Postgres.
5. Apply schema migrations from the owner/migration service. The worker has no
   schema-authority role and exits if required result/attempt tables are absent.
6. Deploy one replica with a unique worker ID.
7. Require a fresh heartbeat whose `sw_version`, machine owner, role, and worker
   ID match the release contract.
8. Verify the worker remains unable to lease while the lane is paused.
9. Arm a separately approved ATS canary only after the deployment and policy
   gates pass.

Do not scale by cloning a service with the same `APPLYPILOT_WORKER_ID`. Each
replica needs an independently stable identity or a launcher that derives and
persists one before the process starts.
