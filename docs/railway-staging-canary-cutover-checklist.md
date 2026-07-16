# Railway Staging and Canary Cutover Checklist

This checklist is the operational boundary after code integration and CI. It is
written for a separate Railway staging project/service and must not be run with
the production project, production DSN, or live worker identities.

## 1. Release identity

- [ ] Record the reviewed Git commit SHA.
- [ ] Build the image from that exact commit and record its immutable image digest.
- [ ] Record the output of:

  ```bash
  python -c "from applypilot.fleet.software_version import current_sw_version; print(current_sw_version())"
  ```

- [ ] Set `APPLYPILOT_RELEASE_VERSION` to that exact value.
- [ ] Record the schema migration checksum and the active brain/policy artifact
  versions. A release without these fields is not promotable.
- [ ] Record the previous known-good image digest and release version for rollback.

## 2. Railway staging isolation

- [ ] Use a distinct Railway project/environment named `applypilot-staging`.
- [ ] Provision a persistent PostgreSQL volume/database for staging. Do not use
  production `DATABASE_PUBLIC_URL` or a shared worker DSN.
- [ ] Provision a staging volume containing only the staging profile and resume.
- [ ] Give every staging worker a unique `APPLYPILOT_WORKER_ID` and mapped login
  role. Never reuse a production worker ID or role.
- [ ] Set `FLEET_MACHINE_OWNER=staging` and
  `APPLYPILOT_FLEET_LABEL=staging`.
- [ ] Set `APPLYPILOT_WORKER_CONTRACT=apply` for ATS staging, or use the separate
  LinkedIn worker image/contract. Do not combine the lanes under one canary.
- [ ] Keep `paused=true`, `ats_paused=true`, and both lane canary capacities at
  zero while migrations and read-only checks run.

## 3. Database and role gates

- [ ] Apply migrations from the owner/migration service, not from a worker.
- [ ] Capture the migration receipt, rollback SQL, and SHA-256 evidence.
- [ ] Verify the mapped role, worker ID, contract, and schema permissions using
  `scripts/bootstrap-fleet-pg-roles.py` and the runtime principal verifier.
- [ ] Confirm no worker can lease while the staging lane is paused.
- [ ] Confirm the staging database contains no production queue, profile, resume,
  token, or email data.

## 4. Staging smoke test

- [ ] Deploy exactly one staging replica from the recorded image digest.
- [ ] Confirm the entrypoint validates the mapped database principal and the
  LiteLLM health endpoint.
- [ ] Confirm a fresh heartbeat reports the exact release version, machine owner,
  worker ID, and contract.
- [ ] Confirm the worker remains unable to acquire a lease with both lanes paused.
- [ ] Run read-only status, queue, schema, policy, and browser-readiness checks.
- [ ] Record logs, heartbeat rows, database identity, image digest, and command
  exit codes in the staging evidence bundle.

## 5. Lane-specific canaries

ATS and LinkedIn are independent release gates. Passing one does not authorize
the other.

### ATS

- [ ] Approve only a reviewed staging batch.
- [ ] Arm the ATS canary with a small explicit `K`.
- [ ] Verify leases are bounded by `K`, then verify automatic pause.
- [ ] Inspect application evidence, duplicate protection, challenge rate, spend,
  error rate, and worker-version consistency.
- [ ] Keep ATS paused until the operator records approval for expansion.

### LinkedIn

- [ ] Verify LinkedIn authentication, challenge state, and browser readiness.
- [ ] Arm only `linkedin_canary_remaining`; do not modify ATS canary fields.
- [ ] Verify the LinkedIn lane auto-stops at its independent `K`.
- [ ] Inspect challenge, duplicate, evidence, and worker-version results.
- [ ] Keep LinkedIn paused until separately approved.

## 6. Promotion or rollback

- [ ] Promote only the image digest whose staging evidence matches the release
  manifest.
- [ ] Preserve the previous digest and release version until the canaries pass.
- [ ] On any failed gate, pause the affected lane, quarantine the staging release,
  and execute only the prewritten, hash-verified rollback procedure.
- [ ] Do not lift a canary, clear an operator pause, or scale workers as a side
  effect of deployment.
- [ ] Archive the final evidence bundle, approval, and rollback predecessor.

## Required evidence bundle

The bundle must contain the commit SHA, image digest, release version, schema and
policy versions, migration receipt and rollback hash, staging project/service
identifiers, worker identities, pause/canary snapshots before and after, heartbeat
observations, smoke-test output, canary observations, and explicit operator
approval. Missing evidence means the release remains blocked.
