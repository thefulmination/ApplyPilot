# Smart Extract Hardening Design

Date: 2026-06-06

## Goal

Make Smart Extract useful as a supplemental job source without allowing it to slow, poison, or destabilize the main discovery pipeline. Structured sources such as Workday, corporate ATS APIs, HiringCafe, public boards, and JobSpy remain the primary discovery path. Smart Extract should fill gaps from less predictable sites.

## Current Behavior

Smart Extract builds targets from `config/sites.yaml` and `.applypilot/searches.yaml`, then uses Playwright to collect page intelligence before asking an LLM to choose an extraction strategy. The current config is conservative: one worker, no network-idle wait, static resource blocking, search sites only, and a capped target count.

The weak points are:

- Every target still pays a browser cost before the LLM strategy step.
- Failures are only run-local log messages, so repeat timeouts are not learned.
- Extracted jobs are saved with minimal quality checks.
- The final summary does not make source health actionable.

## Proposed Architecture

Add three small units around the existing Smart Extract flow.

### HTTP Probe

Before Playwright, fetch the target with plain HTTP and inspect obvious structured data:

- JSON-LD `JobPosting`
- `__NEXT_DATA__`
- embedded job-like JSON
- visible static links that look like job cards

If HTTP probing yields valid jobs, store them and skip Playwright for that target. If it yields enough page intelligence but no jobs, pass that intelligence into the existing strategy path. If it fails, continue to Playwright.

### Source Health

Persist Smart Extract source health in `.applypilot/smartextract_health.json`.

Track:

- source name
- last URL
- last status
- consecutive failures
- timeout count
- jobs found
- last successful strategy
- average runtime seconds
- last checked time

Use this health file to skip sources that repeatedly timeout or fail. Default skip threshold: 3 consecutive hard failures, cooled down for 24 hours. This can be overridden by config.

### Job Validation

Validate Smart Extract jobs before database insertion:

- require a useful title
- require an absolute HTTP(S) URL
- reject navigation/action titles such as `Apply`, `View Job`, `Search`, `Next`, `Sign In`
- reject obvious duplicate URLs within the run
- keep existing location filtering

The validator should return counts by reason so the summary shows whether Smart Extract is finding real jobs or junk.

## Config

Extend the existing `smartextract` section with optional fields:

- `http_probe_enabled`: default `true`
- `health_enabled`: default `true`
- `skip_after_failures`: default `3`
- `skip_cooldown_hours`: default `24`
- `min_valid_jobs_for_success`: default `1`

Existing settings remain valid.

## Data Flow

For each target:

1. Check source health. Skip if the source is cooling down.
2. Run HTTP probe if enabled.
3. If HTTP probe returns valid jobs, store them and record success.
4. Otherwise run the existing Playwright + LLM Smart Extract path.
5. Validate extracted jobs before saving.
6. Record source health and validation counts.
7. Include per-source results in the final log summary.

## Error Handling

Hard failures include timeouts, browser launch errors, parser crashes, and repeated zero-job extraction from a source that previously failed. Soft failures include invalid jobs, filtered location, no matching title, or duplicate URLs.

Hard failures increment source health counters. Soft failures are reported but do not necessarily cool down the source unless no valid jobs are produced repeatedly.

## Tests

Add focused tests for:

- validation rejects navigation/title junk and relative URLs
- health cooldown skips bad sources
- health updates reset consecutive failures after success
- HTTP probe extracts JSON-LD jobs without Playwright
- run summary includes validation and skip counts

## Non-Goals

- Do not increase scraping aggressiveness.
- Do not use logged-in sessions or cookies.
- Do not make Smart Extract the primary discovery source.
- Do not bypass CAPTCHA or bot protections.
