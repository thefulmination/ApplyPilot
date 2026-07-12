# Search Deadlock Retry Design

## Context

Tarpon carries an uncommitted production fix for transient PostgreSQL deadlocks in recurring search leasing and completion. Fleet convergence must preserve that behavior instead of stashing it away during deployment.

## Design

Add one private transaction retry helper in `src/applypilot/fleet/queue.py`. It runs an operation up to four times, catches only `psycopg.errors.DeadlockDetected`, rolls back the failed transaction, and waits with bounded exponential backoff before retrying. The final deadlock and every unrelated exception propagate unchanged.

Wrap `lease_search` and `complete_search` in the helper without changing their SQL, return values, commits, or existing rollback when completion ownership does not match. This keeps retry policy local to the recurring-search transaction boundary where the production deadlock was observed.

## Verification

Unit tests inject deadlocks at the operation boundary and verify rollback, retry count, eventual return value, and final exception propagation. Existing PostgreSQL scheduler and governor tests verify transaction behavior against the real schema. The complete runtime suite must pass before rollout.

## Rollout

Commit and push the tested canonical branch. Redeploy GGGTower and Tarpon from that exact branch. Tarpon's pre-existing tracked edit may be stashed only after the equivalent committed implementation exists remotely. Keep fleet policy versions, pin, pauses, and canaries unchanged.
