# Postgres Canonical Brain Migration Checkpoint

## Objective

Make Postgres the single canonical brain and fleet authority so the same immutable
evidence produces the same recommendation on every machine. Preserve FitMap,
knowledge-graph, pairwise-label, outcome, policy, and decision provenance; keep
SQLite as a rebuildable local cache; and prevent SQLite, a worker, or an incomplete
migration from authorizing applications. Promote ATS and LinkedIn independently
only after backup recovery, import parity, shadow scoring, rollback, and worker
version gates pass.

## Non-Negotiable Invariants

- SQLite remains authoritative until snapshot import, parity, shadow comparison,
  final-delta import, and explicit cutover all pass.
- ATS and LinkedIn remain independently paused through migration and validation.
- Knowledge-graph facts are never rewritten by application outcomes.
- Only a dedicated migration identity may install or upgrade authority schema.
- Tarpon, GGGTower, and other fleet nodes may run read-only verification in
  parallel; schema changes and authority transitions are controller-only.
- Every import batch and parity result is resumable, idempotent, and recorded.

## Current Checkpoint

Date: 2026-07-15

### Continuation 2026-07-15

- No Railway schema, role, ACL, import, authority, or lane mutation has been
  applied. SQLite remains authoritative and both application lanes remain
  stopped with canaries disabled.
- Railway CLI readback confirms the Postgres deployment is running with a
  READY persistent volume mounted at `/var/lib/postgresql/data`: 50,000 MB
  provisioned and 1,127.2192 MB used at the time of inspection.
- Railway PITR is now enabled and independently evidenced. A dedicated
  `Postgres-PITR` bucket was provisioned in `sjc`; all five `WAL_ARCHIVE_*`
  references are present on the Postgres service; deployment
  `4bdd927d-b148-4839-aeef-2b152cbfb4d2` completed successfully; and the running
  PostgreSQL 18.4 instance reports `archive_mode=on`, `archive_timeout=60`, SSL
  enabled, and the pgBackRest archive wrapper as `archive_command`.
- `pg_stat_archiver` reported five archived WAL files, zero failures, and no
  failed-archive timestamp. Direct S3-compatible readback of the dedicated
  bucket found a completed full backup label `20260715-095533F`, 1,295 backup
  objects, and eight archive objects. This proves PITR initialization and WAL
  delivery; it does not replace a restore drill.
- Railway's separate volume-backup control is now enabled and evidenced. A
  manual pre-import backup named `canonical-brain-preimport-20260715` exists as
  backup `be435fdf-d012-45b7-8c0a-28de02992309`, and provider API readback shows
  a `DAILY` schedule (`39 2 * * *`) with provider-managed 518,400-second
  retention. An isolated PITR restore drill then passed at target timestamp
  `2026-07-15T10:00:00Z`: Railway created separate service
  `postgres-restore-drill-20260715` and separate 50 GB volume
  `0418978a-9f0c-4262-9bb1-981962e599ec` without replacing production. The
  unrelated three staged `fleet-worker` changes were not deployed.
- Railway GraphQL inventory resolved volume-instance
  `a977a913-6c24-46a1-befd-c5148b0680e3` and found zero volume-backup schedules
  and zero retained volume backups before provisioning. Post-mutation readback
  proved one retained manual backup and one daily schedule. The ordinary
  volume-restore mutation is not an isolated drill because it replaces the
  source service's mounted volume. The PITR fork was used instead. The restored
  PostgreSQL accepted read-only queries and matched production exactly for all
  three public tables: `apply_queue` 1,000 rows, `fleet_assets` 2 rows, and
  `fleet_config` 1 row, with identical deterministic SHA-256 values per table.
  The temporary service was deleted after verification and its detached volume
  was explicitly marked pending deletion; production remained `SUCCESS`.
- Tarpon and GGGTower were both online and active in the Tailscale control-plane
  readback. No Railway/private-DB gateway peer exists, so external fleet
  connectivity remains a cutover blocker.
- A fresh 2026-07-15 10:02 UTC read-only fleet audit reconfirmed both lanes
  stopped, canaries disabled, capacities null, policy versions null, and
  `spend_cap_usd=0.00`. GGGTower had sixteen fresh idle scoring workers at the
  pinned version and is eligible for advisory compute only. Tarpon was reachable
  through Tailscale but had no fresh compute heartbeat; its stale/version-mismatch
  scoring workers are not eligible. Neither machine is eligible to apply while
  the lane stops remain in force.
- The changed runtime/deployment surface passed four fresh test shards:
  579 passed; 264 passed with 16 environment skips; 104 passed; and 89 passed.
  Total: 1,036 passed, 16 skipped, zero test failures.
- Ruff passed for every changed or untracked Python file; compileall passed;
  Bash syntax passed for one changed script; PowerShell parser validation passed
  for five changed scripts; and `git diff --check` passed.
- Those green checks do not approve the runtime. Independent review proved that
  observing a Codex `item.started` event is not a pre-dispatch MCP gate. A true
  marker boundary must execute before forwarding an interaction to Playwright;
  the current stream-observer implementation remains **NO-GO**.
- Mapped-role rollback also remains **NO-GO** after adversarial PostgreSQL 18.4
  review found lossy membership options, missing PUBLIC database `CREATE`
  restoration, lost mapping `created_at`, incomplete atomic/hash-verified
  break-glass instructions, and an in-doubt receipt crash window. These findings
  are assigned for repair and re-review.
- Follow-up repair closed those five findings and the atomic final-receipt test
  passed, but final code-quality review found a deeper rollback-state design gap:
  outbound memberships are not removed during reconciliation; HBA and rollback
  execution are not bound to the reconciled database and cluster identity; an
  SQL rollback failure can leave already-restored HBA state without an in-doubt
  receipt; Python HBA restore does not preserve and durably flush file metadata;
  and the PowerShell failure path rewrites evidence non-atomically. Do not apply
  this role/HBA workflow live until a transaction-state redesign closes all six
  findings and receives fresh spec and quality approval.

- Runtime branch: `codex/postgres-canonical-brain-phase1`
- Base commit: `962dbaee1a1a41ade6d31eec1d2b67ce4de0f922`
- Worktree:
  `C:\Users\JStal\.config\superpowers\worktrees\ApplyPilot\postgres-canonical-brain-phase1`
- Baseline verification was superseded by adversarial review. On 2026-07-14,
  the brain schema/artifact suite passed 28 tests, but the focused fleet
  role/schema/governor suite failed 5 of 128 tests. The production schema and
  role gate remains **NO-GO** until those failures and review findings are fixed
  and independently re-reviewed.
- Railway Postgres: PostgreSQL 18.4, persistent 50 GB volume, deployment running.
- Railway blockers: the public endpoint accepts plaintext unless clients require
  TLS, and no pooled endpoint exists. Persistent storage, PITR, full pgBackRest
  backup, WAL delivery, a daily volume-backup schedule, a retained manual backup,
  and an isolated PITR restore fork are all proved by provider/database readback.
- Railway artifact bucket: `applypilot-brain-artifacts`
  (`cf90167c-6398-4612-853d-d1c8a69a38ef`, region `sjc`) provisioned for
  staged migration artifacts. Railway bucket objects are
  `committed_unprotected`, not cutover-grade `durable`, until independently
  replicated to immutable/versioned object storage and restore-verified.
- Fresh S3-compatible API readback on 2026-07-15 returned no bucket versioning
  status and empty Object Lock, server-side encryption policy, lifecycle, and
  replication configurations. Do not allow this Railway-only bucket to satisfy
  an artifact authority or policy-release gate.
- Railway's current official Storage Buckets contract explicitly lists object
  versioning, Object Lock, server-side encryption controls, and lifecycle
  configuration as unsupported; it also exposes no customer-controlled
  cross-region replication. Independent immutable storage is therefore a hard
  cutover requirement, not an optional durability enhancement.
- Pre-schema Railway logical backup:
  `C:\Users\JStal\AppData\Local\ApplyPilot\railway-backups\railway-pre-brain-schema-20260714-221825.dump`
  (`447261` bytes,
  SHA-256 `6c014f64f9bc917ed4475f4a1fa269f48219070bceeedbd95b2efae1150a72ca`).
  PostgreSQL 18.4 `pg_restore --list` validation passed.
- Fleet inventory: Tarpon and GGGTower reachable and both at `080c562`, one
  commit behind their configured integration remote.
- Phase 1 draft exists but is not approved and has not been applied to Railway.
- Connection-contract verification currently passes 83 tests with 17 environment-
  dependent skips; artifact-coordinator verification passes 60 tests. Both remain
  under independent adversarial review and are not accepted solely from test counts.
- The latest schema review remains **NO-GO** pending namespace-safe imported
  identities, mandatory parity and release gates, truthful migration transitions,
  database-enforced migrator identity, executable-definition verification,
  partition immutability, durable-artifact proof, and correction-chain/version
  hardening.
- The TypeScript shadow/import boundary was repaired, independently approved, and
  checkpointed on branch `codex/postgres-brain-shadow` at commit `595de31` after
  a fresh 109/109 focused test run and successful typecheck. It still rejects
  `postgres-authoritative` and does not wire authority into scoring or application
  CLIs.
- Approved Python checkpoints now exist on `codex/postgres-canonical-brain-phase1`:
  artifact storage `1d645b4`, authority schema `e513a06`, sealed importer v2
  `054f327`, and cross-language cursor bound `b6cf2f4`.
- The importer v2 has independent approval: 75 focused tests pass after sealed
  backup, crash recovery, memory bounds, and cursor hardening. No snapshot has
  been imported.
- Approved TypeScript shadow checkpoints exist on `codex/postgres-brain-shadow`:
  initial shadow boundary `595de31` and exact Python-manifest-v2 handoff `9f802a1`.
  The latter passed 145 focused tests plus typechecking and still exposes no
  Postgres-authoritative scoring/application mode.
- Fleet runtime and deployment hardening remain uncommitted and **NO-GO** pending
  final independent mapped-role reviews. Known production paths are being verified
  under distinct per-node SCRAM identities; no live role, ACL, HBA, or Railway
  mutation has occurred.

## Live Authority And Lane Safety 2026-07-14

- The configured fleet authority is local PostgreSQL database
  `applypilot_fleet` on loopback (`::1`), not the Railway database.
- Railway PostgreSQL is running 18.4 with SSL and PITR enabled, but it still has
  no `brain_schema_versions` table and only the legacy global `paused` gate. It
  is therefore not the fleet or brain authority.
- The Railway public TCP proxy is encrypted but is not currently suitable for
  authenticated fleet authority traffic. Live `verify-full` with system roots
  failed; the presented leaf certificate had `CN=localhost`, SAN `localhost`,
  issuer `root-ca`, and SHA-256 fingerprint
  `EE:78:20:06:23:9D:29:E6:24:35:F0:22:99:C8:75:42:AD:58:D7:65:73:A9:AC:62:F2:83:96:D9:75:98:7D:97`.
  Treat that fingerprint as time-specific evidence, not a permanent pin.
  Railway-internal services may use private networking. External fleet nodes
  require an authenticated Tailscale/private gateway or a provider endpoint
  whose certificate validates its public hostname before cutover.
- Current Tailscale inventory has four peers (GGGTower, Tarpon, Paloma's
  MacBook Air, and the HP EliteBook) plus the controller. No Railway/private-DB
  gateway peer exists yet. A gateway must be provisioned, identity-pinned, and
  tested from Tarpon and GGGTower before public-proxy access can be removed.
- Current provider/runtime research recommends two Railway-hosted Tailscale
  userspace gateways advertising one stable Tailscale Service and forwarding
  through session-mode PgBouncer to `Postgres.railway.internal`. This keeps the
  database private, preserves per-node PostgreSQL identities, and removes a
  single gateway host from the connection contract. It is a proposed design,
  not an implemented or approved release path.
- The live Postgres budget is 500 total connections with 3 superuser reserves,
  but runtime inspection found workers currently reconnect on each polling
  cycle despite the runbook's long-lived-connection claim. Session pooling is
  therefore justified for churn control; transaction pooling remains out of
  scope until transaction-local role/search-path behavior is proved compatible.
- The gateway release proof must include two healthy service backends, tailnet
  grants restricted to approved fleet identities and TCP 6432, private Postgres
  resolution/query from each gateway, unique mapped-role proofs from Tarpon and
  GGGTower, a paused peak-concurrency soak, single-gateway failover, and removal
  of shared/admin DSNs from every remote launcher and secret store.
- The local fleet row was found globally paused with ATS operator-paused, but
  LinkedIn still had latent canary capacity (`99916`) and both lane modes were
  `canary` while both policy versions were null.
- A guarded single transaction, requiring both `paused=TRUE` and
  `ats_paused=TRUE`, set both lane modes to `stopped`, disabled both canaries,
  and cleared both remaining-capacity values. A separate connection read back:
  global pause true, ATS pause true with source `operator`, both lane modes
  stopped, both canaries false, both capacities null, and both policy versions
  null.
- Do not infer full Railway cutover readiness from provider recovery controls.
  Provider recovery is now proved, but connection identity/TLS strategy, schema
  readiness, artifact durability, import parity, and shadow parity remain open.

## Immutable SQLite Snapshot 2026-07-14

- Local path:
  `C:\Users\JStal\AppData\Local\ApplyPilot\brain-backups\applypilot-brain-20260714-215823.db`
- OneDrive mirror: `C:\Users\JStal\OneDrive\Documents\ApplyPilot-Backups`
- Bytes: `7813488640`
- SHA-256: `cc8b4c1e69f373e2c25ea365075161112934d757405085f63a587cce9d3b8a74`
- SQLite quick check: `ok`
- Page size: `4096`
- Page count: `1907590`

Authoritative snapshot counts:

| Source table | Rows |
| --- | ---: |
| jobs | 101679 |
| applications | 1077 |
| application_events | 1088 |
| email_events | 2109 |
| email_event_reviews | 93 |
| reviewed_outcomes | 42 |
| research_labels | 2167 |
| research_label_confidence | 2023 |
| research_pairwise_labels | 133 |
| research_kg_artifacts | 1 |
| research_kg_runs | 0 |
| research_scores | 0 |
| decision_policy_versions | 14 |
| job_decisions | 701658 |

This snapshot is the Phase 2 import source. The live SQLite file is newer and
remains authoritative until final-delta cutover. Any import run must bind the
exact byte length and SHA-256 above and must reject another source fingerprint.

## Review Failures To Correct

- Enforce a dedicated migration role; generic `CREATE` on `public` is insufficient.
- Verify the full schema contract, not only table and column names.
- Preserve existing canonical enums and nullable expiry without lossy conversion.
- Add canonical job aliases and source-item identities.
- Preserve reviewed-outcome email attribution and adjudication fields.
- Add stable application entities plus append-only application events.
- Model role-keyed policy artifacts, owner approvals, and release gates.
- Use policy-aligned decision partitions and globally unique decision identities.
- Make artifacts, ledgers, decisions, archive manifests, and migration/parity
  evidence immutable.
- Add migration sources, batches/checkpoints, quarantines, parity runs, and
  parity results before importing data.
- Remove broad default worker grants before authority tables are installed.

## Stage Gates

1. **Provider recovery controls**
   - PASS: persistent storage proved.
   - PASS: PITR and volume backup schedule enabled and evidenced.
   - PASS: isolated PITR restore drill passed with table-level content hashes.
   - OPEN: TLS/private connection strategy and connection budgets must be
     documented, implemented, and proved from Tarpon and GGGTower.

2. **Authority schema**
   - Disposable-Postgres tests pass.
   - Independent spec and security reviews approve.
   - Worker cannot read or mutate authority tables.
   - Fleet state remains unchanged.

3. **Resumable importer**
   - Immutable SQLite snapshot fingerprint recorded.
   - Keyset batches are idempotent and retryable.
   - Unresolved references are quarantined, never invented.

4. **Parity**
   - Counts, membership hashes, full small-ledger hashes, sampled row hashes,
     policy/decision hashes, foreign keys, aliases, outcomes, and lane mapping pass.

5. **Shadow scoring**
   - TypeScript and Python canonical hashes match.
   - Every Postgres decision is compared with the SQLite-authoritative result.
   - All mismatches are resolved or explicitly block cutover.

6. **Cutover**
   - SQLite writers freeze.
   - Final delta imports and parity reruns.
   - TS scoring and Python promotion switch to Postgres-only authority.
   - SQLite production writes are rejected.

7. **Lane release**
   - ATS and LinkedIn validate independently while paused.
   - Policies promote independently.
   - Small lane-specific canaries arm only after all preceding gates pass.

## Resume Instructions

Start by reading this file, checking `git status`, and verifying Railway and fleet
state live. Never infer a completed gate from this checkpoint alone; rerun the
listed evidence command or inspect the recorded immutable receipt.
