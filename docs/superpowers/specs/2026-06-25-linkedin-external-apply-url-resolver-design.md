# LinkedIn External Apply URL Resolver Design

## Goal

Add a safe, read-only LinkedIn resolver that converts LinkedIn-sourced jobs with external "Apply" destinations into normal offsite ATS targets by backfilling `jobs.application_url`.

The immediate purpose is to reduce the number of jobs that ApplyPilot treats as LinkedIn-paced Easy Apply or unresolved LinkedIn jobs. Once an offsite ATS URL is saved in `application_url`, existing apply queue logic already routes that job through the faster external lane and excludes it from LinkedIn daily caps.

## Current Context

ApplyPilot already has the key pieces this feature should reuse:

- `applypilot linkedin-split` reports LinkedIn jobs as either offsite external lane or Easy Apply / unresolved based on whether `application_url` points away from LinkedIn.
- `src/applypilot/apply/chrome.py` can launch an isolated browser worker from a cloned real Chrome or Edge profile, preserving normal login state without raw cookie import.
- `src/applypilot/apply/launcher.py` already has LinkedIn daily caps, LinkedIn cooldowns, host gaps, jitter, and a shared `_LINKEDIN_LANE_SQL` idea: external `application_url` takes precedence over the source `url`.
- `jobs.application_url` is the existing column used by enrichment, readiness checks, apply tracking, and exports.

The resolver should therefore add only the missing step: visit LinkedIn job pages with a logged-in browser profile and resolve external apply destinations before the apply stage runs.

## Recommended Approach

Implement a dedicated CLI command:

```powershell
.\.venv\Scripts\python.exe -m applypilot linkedin-resolve-apply-urls --limit 200 --delay-min 8 --delay-max 20 --tiers priority,recommended
```

The command will:

1. Select unresolved LinkedIn jobs from the database.
2. Open each LinkedIn job page in a visible, logged-in cloned Chrome or Edge profile.
3. Detect whether the page is Easy Apply, external Apply, unavailable, login-gated, or challenge-gated.
4. For external Apply only, capture the final offsite URL and write it to `jobs.application_url`.
5. Store resolver status and error metadata so the command can resume safely.
6. Stop the run immediately on LinkedIn checkpoint, captcha, unusual-activity, restricted-account, or login-wall signals.

This is the best fit because it uses LinkedIn only for URL resolution, not bulk applying, and lets existing offsite ATS automation handle the actual application workflow.

## Alternatives Considered

### Extend Smart Extract

Smart Extract could try to resolve LinkedIn apply URLs during enrichment. This would reuse existing scraping infrastructure, but it mixes a logged-in account workflow into a broad discovery/enrichment stage. That makes pacing, challenges, and account safety harder to reason about.

### Resolve URLs Inside Apply

The apply agent could open LinkedIn, click Apply, and continue if redirected offsite. This reduces a separate command, but it wastes expensive agent cycles and entangles resolution with real application submission.

### Raw Cookie Import

Importing `linkedin_cookies.txt` directly is not recommended. Browser cookies can be encrypted, incomplete, stale, and suspicious when replayed outside the normal browser profile. A cloned logged-in browser profile is safer and matches the way ApplyPilot already works.

## Data Model

Keep `jobs.application_url` as the authoritative resolved apply target.

Add resolver metadata columns to `jobs` through the central `_ALL_COLUMNS` migration registry:

- `linkedin_resolved_at TEXT`
- `linkedin_resolve_status TEXT`
- `linkedin_resolve_error TEXT`
- `linkedin_resolve_attempts INTEGER DEFAULT 0`
- `linkedin_resolve_final_url TEXT`

Status values:

- `resolved_offsite`: external apply URL saved to `application_url`.
- `easy_apply`: LinkedIn Easy Apply detected; no offsite URL exists.
- `login_required`: LinkedIn session was not available.
- `challenge_required`: checkpoint, captcha, unusual activity, or account restriction detected; the run stops.
- `unavailable`: LinkedIn says the job is no longer available.
- `no_apply_button`: page loaded but no usable apply control was found.
- `timeout`: page or click exceeded the configured timeout.
- `error`: unexpected resolver failure.

The command must never overwrite an existing non-LinkedIn `application_url` unless `--refresh` is passed.

## Job Selection

Default selection:

```sql
WHERE (lower(site) = 'linkedin' OR url LIKE '%linkedin.com/jobs%')
  AND duplicate_of_url IS NULL
  AND COALESCE(liveness_status, '') != 'dead'
  AND applied_at IS NULL
  AND (
    application_url IS NULL
    OR application_url = ''
    OR application_url LIKE '%linkedin.com%'
  )
```

Default priority order:

1. `audit_label IN ('priority', 'recommended')`
2. Higher `audit_score`
3. Higher `fit_score`
4. Most recent `discovered_at`

Options:

- `--limit`: maximum rows considered in one run.
- `--tiers`: comma-separated audit labels, default `priority,recommended`.
- `--include-low`: include `review` and `low` labels.
- `--refresh`: revisit rows that already have resolver status.
- `--dry-run`: show rows that would be visited without opening LinkedIn.

## Browser Behavior

Use the same visible browser profile model as apply workers:

- Launch with `apply.chrome.launch_chrome`.
- Default to browser `chrome`.
- Connect to the launched browser over CDP from Playwright.
- Default to worker id `80` so the resolver does not collide with normal apply workers.
- Reuse the cloned profile so LinkedIn sees the normal logged-in session.
- Do not run headless by default.

The resolver should not use raw cookie files and should not attempt to bypass LinkedIn anti-abuse systems.

## Resolution Flow

For each job:

1. Navigate to `jobs.url`.
2. Check for stop conditions:
   - URL contains `/checkpoint/` or `/uas/`.
   - Page text includes "unusual activity", "verify it's you", "quick security check", "restricted your account", or similar.
   - Login page or sign-in prompt is shown.
   - CAPTCHA is present.
3. If stopped by challenge or login wall, persist status and stop the whole run.
4. If unavailable, persist `unavailable` and continue.
5. Find the primary apply control.
6. If the control is Easy Apply, persist `easy_apply` and continue.
7. If the control is external Apply:
   - Click the control.
   - Capture any newly opened page or same-tab navigation.
   - Follow redirects until network idle or a short timeout.
   - If final host is not LinkedIn, save it to `application_url` and `linkedin_resolve_final_url`.
   - Return to LinkedIn or close the new tab.

The resolver must not submit an application, fill fields, click Easy Apply "Next", upload files, answer questions, or continue past the initial external redirect.

## Safety Defaults

Default safety settings:

- One browser worker.
- Visible browser.
- `--delay-min 8`, `--delay-max 20`.
- Page timeout around 45 seconds.
- Click or redirect timeout around 20 seconds.
- Stop entire run on challenge, login wall, CAPTCHA, or restriction.
- Do not run while an apply command is actively using the same worker id.

Environment variables:

- `APPLYPILOT_LINKEDIN_RESOLVE_LIMIT`
- `APPLYPILOT_LINKEDIN_RESOLVE_DELAY_MIN`
- `APPLYPILOT_LINKEDIN_RESOLVE_DELAY_MAX`
- `APPLYPILOT_LINKEDIN_RESOLVE_PAGE_TIMEOUT`
- `APPLYPILOT_LINKEDIN_RESOLVE_CLICK_TIMEOUT`
- `APPLYPILOT_LINKEDIN_RESOLVE_BROWSER`
- `APPLYPILOT_LINKEDIN_RESOLVE_WORKER_ID`, default `80`

## Files To Add Or Change

Expected implementation files:

- Add `src/applypilot/linkedin_resolver.py` for selection, browser workflow, classification, persistence, and result summary.
- Update `src/applypilot/cli.py` with `linkedin-resolve-apply-urls`.
- Update `src/applypilot/database.py` with resolver columns.
- Add `tests/test_linkedin_resolver.py` for pure selection, URL classification, status updates, and CLI dry-run behavior.
- Add a short docs note or command hint near the existing LinkedIn split output.

## Testing Strategy

Automated tests should avoid live LinkedIn network access.

Testable units:

- External vs LinkedIn URL classification.
- Query selection excludes dead, duplicate, applied, and already-offsite rows.
- Query ordering prioritizes `priority` and `recommended` labels.
- Status persistence increments attempts and preserves previous good `application_url`.
- `--dry-run` prints candidates without launching a browser.
- Challenge and login statuses are treated as stop-the-run statuses.

Manual verification after implementation:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_linkedin_resolver.py tests/test_apply_linkedin_cap.py
.\.venv\Scripts\python.exe -m applypilot linkedin-resolve-apply-urls --dry-run --limit 20
.\.venv\Scripts\python.exe -m applypilot linkedin-split
```

A live run should start small:

```powershell
.\.venv\Scripts\python.exe -m applypilot linkedin-resolve-apply-urls --limit 25 --delay-min 12 --delay-max 30 --tiers priority,recommended
.\.venv\Scripts\python.exe -m applypilot linkedin-split
```

## Success Criteria

The feature is successful when:

- External LinkedIn apply buttons increase the offsite count in `applypilot linkedin-split`.
- Easy Apply jobs remain LinkedIn-paced and are not misclassified as offsite.
- A challenge or login wall stops the run before repeated account-risking page hits.
- Existing apply logic starts treating resolved external LinkedIn jobs as normal offsite ATS jobs without further changes.
- A crashed resolver run can resume without losing progress or revisiting successful rows by default.

## Non-Goals

- Do not automate LinkedIn Easy Apply submission in this feature.
- Do not bypass CAPTCHA, checkpoint, MFA, or account restrictions.
- Do not import raw LinkedIn cookies from text files.
- Do not change scoring, tailoring, or recommendation logic.
- Do not create multiple LinkedIn resolver workers until the single-worker version proves safe.
