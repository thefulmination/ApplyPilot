# ApplyPilot Parallel Release Remediation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clear the remaining Python and container-build release gates quickly without overlapping edits or changing live fleet state.

**Architecture:** Every lane starts from the same pinned runtime candidate and owns a disjoint file set. Claude produces isolated commits; Codex reviews and integrates them into `codex/cloud-cutover-runtime-integration`, runs the combined suite, builds the image, and makes the release decision.

**Tech Stack:** Python 3.12, pytest, PowerShell 7, GitHub Actions, Docker, PostgreSQL, SQLite test fixtures.

---

## Shared Baseline And Guardrails

- Base branch: `codex/cloud-cutover-runtime-integration`
- Runtime code base: `f9e998937da79d21bda05c6e4271873364d4c943`
- The published Claude lane heads include one later docs-only coordination commit containing this plan; no runtime code differs from `f9e9989` at lane creation.
- Verified focused result: `python -m pytest -q tests/test_emergency_legacy_authority_containment.py` -> `532 passed`
- Known broad result: `python -m pytest -n 4 -q` -> `3943 passed, 17 skipped, 34 failed`
- Known serial sample: ResBuild, fleet-version, and agent-selection files -> `24 passed, 19 failed`
- Do not deploy, unpause workers, issue fleet commands, mutate production PostgreSQL, read or change secrets, push container images, or merge into `main`.
- Do not weaken Postgres authority, admission gates, version pins, challenge handling, or evidence verification merely to make an old test pass.
- Each lane must commit only its owned files and report the commit SHA, changed files, exact commands and results, root cause, and residual risk.

## File Ownership Matrix

| Lane | Owner | Branch | Allowed writes | Forbidden writes |
|---|---|---|---|---|
| C1 ResBuild authority | Claude | `claude/resbuild-postgres-authority` | `src/applypilot/resbuild_bridge.py`, `tests/test_resbuild_bridge.py` | All other runtime and test files |
| C2 Container gate | Claude | `claude/container-build-gate` | `Dockerfile`, `.dockerignore`, `.github/workflows/ci.yml`, `tests/test_container_build_contract.py` if needed | Runtime Python, migrations, deployment secrets |
| C3 Apply-runtime triage | Claude | `claude/apply-runtime-triage` | `docs/superpowers/evidence/2026-07-16-distributed-consolidation/claude-apply-runtime-triage.md` only | All source, tests, workflows, and fleet state |
| X1 Apply/auth repair | Codex | integration worktree | `src/applypilot/apply/launcher.py`, `src/applypilot/apply/greenhouse_submit.py`, `src/applypilot/inbox_auth.py`, directly associated tests | ResBuild and CI files while Claude owns them |
| X2 Fleet regression repair | Codex | integration worktree | fleet worker, queue, browser-preflight, console challenge, version script, directly associated tests | ResBuild and CI files while Claude owns them |
| X3 Integration/release | Codex | integration worktree | merge conflict resolution, evidence manifest/report | Live deployment until every release gate is green |

## Task C1: Claude ResBuild Postgres Authority

**Files:**

- Modify: `src/applypilot/resbuild_bridge.py`
- Modify: `tests/test_resbuild_bridge.py`

- [ ] **Step 1: Reproduce the complete lane failure set**

Run:

```powershell
python -m pytest -q tests/test_resbuild_bridge.py
```

Expected baseline: failures where legacy SQLite fixtures reach `pgqueue.connect()` with the passwordless default DSN.

- [ ] **Step 2: Establish the authority contract from current code and tests**

Document in the commit message which operations are authoritative writes, which are dry-run calculations, and whether SQLite is only a disposable compatibility fixture. Do not add an automatic production fallback from Postgres to SQLite.

- [ ] **Step 3: Add or correct failing tests before production edits**

The tests must explicitly inject the authority dependency they exercise. A test that expects PostgreSQL behavior must provide a fake PostgreSQL connection; a compatibility-unit test may inject a repository/connection adapter but may not rely on ambient DSN defaults.

- [ ] **Step 4: Implement the smallest authority-safe repair**

Keep production promotion and reversion authoritative in Postgres. Make dependency injection explicit enough that local SQLite fixtures cannot accidentally invoke the real fleet DSN.

- [ ] **Step 5: Verify the lane**

Run:

```powershell
python -m pytest -q tests/test_resbuild_bridge.py
python -m ruff check src/applypilot/resbuild_bridge.py tests/test_resbuild_bridge.py
git diff --check
```

Expected: all ResBuild tests pass, Ruff passes for owned files, and no whitespace errors.

- [ ] **Step 6: Commit the lane**

```powershell
git add src/applypilot/resbuild_bridge.py tests/test_resbuild_bridge.py
git commit -m "fix: isolate resbuild postgres authority tests"
```

## Task C2: Claude Container Build Gate

**Files:**

- Modify: `Dockerfile`
- Modify: `.dockerignore` only if required
- Modify: `.github/workflows/ci.yml`
- Create: `tests/test_container_build_contract.py` only if a static contract test materially protects the workflow

- [ ] **Step 1: Inspect the current image contract without building**

Confirm the Python version, package installation path, entrypoint, non-root behavior, health/smoke command, and files excluded from the image. Record any mismatch in the commit message.

- [ ] **Step 2: Add a non-publishing CI image build**

The job must build the candidate image on GitHub-hosted Linux, must not log in to a registry, must not push an image, and must run a deterministic smoke command from the built image.

- [ ] **Step 3: Keep CI triggers bounded**

The image job may run on pull requests, workflow dispatch, and relevant branch/path changes. It must not deploy or access repository environments/secrets.

- [ ] **Step 4: Verify static configuration locally**

Run:

```powershell
python -c "import pathlib, yaml; yaml.safe_load(pathlib.Path('.github/workflows/ci.yml').read_text(encoding='utf-8')); print('workflow yaml ok')"
python -m pytest -q tests/test_container_build_contract.py
git diff --check
```

If no contract test is added, omit only that pytest command and explain why the workflow itself is sufficient.

- [ ] **Step 5: Commit without dispatching CI**

```powershell
git add Dockerfile .dockerignore .github/workflows/ci.yml
if (Test-Path tests/test_container_build_contract.py) { git add tests/test_container_build_contract.py }
git commit -m "ci: add non-publishing container build gate"
```

Do not open a pull request or dispatch the workflow. Codex will review, integrate, and trigger the gate.

## Task C3: Claude Apply-Runtime Read-Only Triage

**Files:**

- Create: `docs/superpowers/evidence/2026-07-16-distributed-consolidation/claude-apply-runtime-triage.md`

- [ ] **Step 1: Reproduce each cluster serially**

Run each file independently and record exact failing test names and trace roots:

```powershell
python -m pytest -q tests/test_apply_agent_selection.py
python -m pytest -q tests/test_apply_channel.py
python -m pytest -q tests/test_console_challenge_actions.py
python -m pytest -q tests/test_browser_readiness_preflight.py
python -m pytest -q tests/test_fleet_apply_home.py tests/test_fleet_apply_lane.py
python -m pytest -q tests/test_gmail_reauth.py tests/test_greenhouse_submit.py
python -m pytest -q tests/test_inbox_auth_mail_source.py tests/test_inbox_auth_redaction.py
```

- [ ] **Step 2: Group failures by root cause**

For every group, identify the first bad call or state transition, the likely owning production function, whether the test or production behavior is stale, and the smallest safe repair. Explicitly flag shared-file overlap in `launcher.py`.

- [ ] **Step 3: Write the evidence report only**

The report must include commands, counts, failing test names, source file/function references, dependency order, and recommended Codex ownership. Do not edit source or tests in this lane.

- [ ] **Step 4: Commit the report**

```powershell
git add docs/superpowers/evidence/2026-07-16-distributed-consolidation/claude-apply-runtime-triage.md
git commit -m "docs: triage remaining apply runtime regressions"
```

## Task X1-X2: Codex-Owned Repairs

Codex will use Claude's C3 diagnosis, but will retain ownership of overlapping apply/auth and fleet files. Each root-cause group gets a red focused test, minimal fix, focused green run, and commit before the next group. Codex will not touch C1 or C2 owned files until Claude returns them.

## Task X3: Integration And Release Gates

- [ ] Review every Claude diff rather than trusting the summary.
- [ ] Cherry-pick C1, run `tests/test_resbuild_bridge.py`, and inspect Postgres authority behavior.
- [ ] Cherry-pick C2, review workflow permissions/triggers, then dispatch the non-publishing build.
- [ ] Consume C3 diagnostics; cherry-pick its report only after verifying command evidence.
- [ ] Run focused tests for every repaired cluster.
- [ ] Run `python -m ruff check src`.
- [ ] Run the complete Python suite serially in the release environment.
- [ ] Run the complete suite in CI's supported matrix; do not require xdist unless test isolation is explicitly repaired.
- [ ] Require a successful container image build and smoke test.
- [ ] Update the compatibility manifest and final consolidation report with exact commit, commands, counts, and workflow run URL.
- [ ] Keep production application lanes paused until the user separately approves staging/canary deployment.

## Claude Launch Prompts

### C1 Prompt

```text
Work only on the already-published branch claude/resbuild-postgres-authority. Its runtime code base is f9e998937da79d21bda05c6e4271873364d4c943 and its additional initial commit is documentation only. Follow Task C1 in docs/superpowers/plans/2026-07-16-parallel-release-remediation.md. You may write only src/applypilot/resbuild_bridge.py and tests/test_resbuild_bridge.py. Preserve Postgres as canonical authority and never add automatic SQLite production fallback. Do not touch fleet state, secrets, deployments, other source/tests, or main. Commit the result and return SHA, root cause, changed files, exact test/lint results, and residual risks.
```

### C2 Prompt

```text
Work only on the already-published branch claude/container-build-gate. Its runtime code base is f9e998937da79d21bda05c6e4271873364d4c943 and its additional initial commit is documentation only. Follow Task C2 in docs/superpowers/plans/2026-07-16-parallel-release-remediation.md. You may write only Dockerfile, .dockerignore, .github/workflows/ci.yml, and tests/test_container_build_contract.py if needed. Add a non-publishing build and deterministic smoke gate. Do not use secrets, deploy, push an image, dispatch CI, or touch runtime Python. Commit and return SHA, changed files, exact validation results, and residual risks.
```

### C3 Prompt

```text
Work only on the already-published branch claude/apply-runtime-triage. Its runtime code base is f9e998937da79d21bda05c6e4271873364d4c943 and its additional initial commit is documentation only. Follow Task C3 in docs/superpowers/plans/2026-07-16-parallel-release-remediation.md. This is read-only diagnosis except for the single allowed evidence report. Do not edit source, tests, workflows, fleet state, secrets, or deployment files. Commit only the report and return SHA plus exact test commands/counts and dependency-ordered root causes.
```
