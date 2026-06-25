# Cloud Apply Fleet — Design Spec (queue-offload)

Date: 2026-06-24
Status: Draft for implementation
Owner: Jonathan Stallone
Scope: Offsite ATS apply fleet on Railway, billed via a metered API key — provider (Anthropic/Sonnet vs the ~10x-cheaper DeepSeek) decided by the POC A/B (§3e, §7).

All file:line citations are against the LIVE Python tool at
`C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot` unless noted.
Authoritative home DB: `C:/Users/JStal/AppData/Local/ApplyPilot/applypilot.db`.

---

## 1. Overview & Goals

### Goal
Stand up a cloud "apply fleet" of stateless Railway containers that submit applications to
**offsite ATS jobs only** (Greenhouse / Lever / Ashby / Workable / Workday / amazon.jobs /
greenhouse.io / ashbyhq.com / lever.co etc.) **in parallel**, billed against a **metered API
key** (NOT Jonathan's Claude subscription; the model provider — Anthropic/Sonnet vs the
~10x-cheaper DeepSeek — is decided by the POC A/B, §3e), with a hard global spend cap and a
POC gate before any scale-out.

### Non-goals / explicit scope
- **Offsite-only. No LinkedIn, ever, in the fleet.** LinkedIn applies require a logged-in
  session (the `li_at` cookie), a cloned Chrome profile, and carry a catastrophic ban risk if
  run from datacenter IPs at volume. The home box owns all of that
  (`chrome.py` `setup_worker_profile` / `linkedin_login`). The fleet has **no cookies, no
  profile, no LinkedIn lane** — it only sees `http(s)` ATS apply targets that are not
  `linkedin.com`. This keeps account-safety risk concentrated at home where it is already
  managed (rolling-24h LinkedIn cap, gap-jitter, same-day halt).
- **No scoring, enrichment, resolver, or the 60-column `jobs` schema in the cloud.** The home
  PC stays the brain. Only ~6 routing columns per job cross the wire.
- **Do not rewrite the apply agent.** Reuse `build_apply_agent_command`
  (`launcher.py:185`), `_make_mcp_config` (`launcher.py:101`), the stream-json result/cost
  parser (`launcher.py:1098-1191`), and the result classifier (`launcher.py:1289-1319`).

### Why this is safe to do
- The agent runs `--permission-mode bypassPermissions` over attacker-influenceable ATS page
  content, BUT `--disallowedTools` (`launcher.py:203-221`) locks it to only the Playwright MCP
  tools — it cannot read local files (no resume/profile/env exfil), write, execute, or browse
  outside the driven browser. This lockdown is honored even under bypass and matters MORE
  offsite than at home.
- Cost is the real exposure: Railway compute is tens of dollars; the Anthropic API is ~$2-3k.
  A durable global spend cap (`fleet_config.spend_cap_usd`) gates every lease.

### Home baseline to beat
Live `apply_status` distribution at design time: applied=138 / deferred=112 /
auth_required=78 / failed=57 / in_progress=1. The home **attempt-success baseline is ~36%**
(applied / attempted). The POC must not materially underperform this (see §7).

---

## 2. Architecture — queue-offload (text diagram)

```
            HOME PC  (the brain — UNCHANGED, 60-col schema never leaves)
            ===============================================================
  resolver / scorer / enrichment / LinkedIn resolver
                       |
                       v
            SQLite  applypilot.db   (60-col jobs, profile, resumes, llm_usage, applications)
                       |
       PUSH (offsite-eligible only)  ^  PULL (terminal results)
       ~6 cols per job               |  url + status + cost + timing
                       v             |
  - - - - - - - - - - -|- - - - - - -|- - - - - - - -  Railway private/public network
                       v             |
            RAILWAY POSTGRES (managed)   <----- thin state only ----->
            ===============================================================
              apply_queue   (queued | leased | applied | failed | blocked | crash_unconfirmed)
              fleet_config  (spend_cap_usd, paused)   [single row id=1]
                       ^
        SELECT ... FOR UPDATE SKIP LOCKED ORDER BY score DESC   (lease)
        UPDATE ... SET status=..., est_cost_usd=...             (result write)
                       |
                       v
            RAILWAY FLEET  (N stateless containers, Hobby <=6 replicas)
            ===============================================================
   each replica = 1 worker:
     loop:
       check fleet_config (paused? SUM(est_cost_usd) >= cap?)  --> halt
       lease 1 job (SKIP LOCKED, top score, host-polite)
       launch headless chromium on CDP port (BASE_CDP_PORT=9222, --no-sandbox)
       run EXISTING apply agent  (claude CLI -p, sonnet, Playwright MCP @0.0.76)
       parse stream-json: RESULT:APPLIED/EXPIRED/CAPTCHA/... + total_cost_usd
       write result + cost back to apply_queue (lease_owner-guarded)
     on startup + periodically: reclaim_stale_leases  --> crash_unconfirmed
```

Key boundary: **home is authoritative**. Postgres is a denormalized work queue + result
mailbox. If Postgres is lost, nothing important is lost — home re-pushes.

---

## 3. Components

### 3a. Postgres `apply_queue` + `fleet_config` (real DDL)

```sql
-- ===========================================================================
-- apply_queue : the thin offsite-apply work queue on Railway Postgres.
-- One row per offsite-appliable job. url is the cross-system key + idempotency anchor.
-- ===========================================================================
CREATE TYPE apply_queue_status AS ENUM (
    'queued',             -- pushed, eligible to lease
    'leased',             -- a worker holds it (lease_expires_at in the future)
    'applied',            -- submit confirmed
    'failed',             -- terminal non-submit (expired/captcha/page_error/...)
    'blocked',            -- site/cloudflare/auth wall the offsite agent can't pass
    'crash_unconfirmed'   -- worker died mid-job, possibly post-submit: NEVER re-leased
);

CREATE TABLE apply_queue (
    -- ---- identity / job columns (pushed from home) ------------------------
    url                     TEXT        PRIMARY KEY,          -- = jobs.url
    company                 TEXT,
    title                   TEXT,
    application_url         TEXT        NOT NULL,             -- offsite ATS form target
    score                   REAL        NOT NULL,            -- COALESCE(audit_score, fit_score)
    apply_domain            TEXT,                            -- effective apply host (politeness key)

    -- ---- queue / lease state ---------------------------------------------
    status                  apply_queue_status NOT NULL DEFAULT 'queued',
    lease_owner             TEXT,
    lease_expires_at        TIMESTAMPTZ,
    last_attempted_at       TIMESTAMPTZ,                     -- set at lease time (politeness)
    attempts                INTEGER     NOT NULL DEFAULT 0,

    -- ---- result columns (written by the fleet) ---------------------------
    apply_status            TEXT,                            -- raw agent outcome: applied / failed:<reason> / expired / captcha ...
    apply_error             TEXT,
    verification_confidence TEXT,                            -- pass-through (NULL in live DB today)
    agent_model             TEXT,                            -- provider/model that ran this job (§3e A/B breakdown)
    est_cost_usd            NUMERIC(10,4),                   -- apply-agent total_cost_usd (drives the cap)
    applied_at              TIMESTAMPTZ,
    worker_id               TEXT,
    apply_duration_ms       INTEGER,

    -- ---- bookkeeping ------------------------------------------------------
    pushed_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    synced_to_home_at       TIMESTAMPTZ                      -- set by PULL; NULL = not yet ingested home
);

-- Lease query index: WHERE status='queued' ORDER BY score DESC LIMIT 1.
CREATE INDEX idx_apply_queue_lease
    ON apply_queue (score DESC)
    WHERE status = 'queued';

-- Reclaim scan: leased rows whose lease has expired.
CREATE INDEX idx_apply_queue_reclaim
    ON apply_queue (lease_expires_at)
    WHERE status = 'leased';

-- PULL scan: terminal rows not yet ingested back into home SQLite.
CREATE INDEX idx_apply_queue_unsynced
    ON apply_queue (updated_at)
    WHERE status IN ('applied','failed','blocked','crash_unconfirmed')
      AND synced_to_home_at IS NULL;

-- Politeness scan: recently-touched domains.
CREATE INDEX idx_apply_queue_host_recent
    ON apply_queue (apply_domain, last_attempted_at);
```

`score` is `REAL` (not INTEGER) because home `audit_score` is REAL; truncating corrupts the
tie-break ordering used by `acquire_job` (`launcher.py` `ORDER BY COALESCE(audit_score, fit_score) DESC`,
~line 615). The ENUM is preferred over a CHECK because it is referenced by name in the
lease/reclaim SQL.

```sql
-- Single-row global control: spend cap + kill switch. id=1 enforced.
CREATE TABLE fleet_config (
    id              INTEGER       PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    spend_cap_usd   NUMERIC(10,2) NOT NULL DEFAULT 0,   -- 0 = no cap; halt when SUM(est_cost_usd) >= this
    paused          BOOLEAN       NOT NULL DEFAULT FALSE, -- global kill switch
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

INSERT INTO fleet_config (id, spend_cap_usd, paused)
VALUES (1, 0, FALSE)
ON CONFLICT (id) DO NOTHING;
```

### 3b. SQLite <-> PG fleet-sync (real SQL)

**PUSH Step A — SELECT offsite-appliable jobs from home SQLite** (mirrors `acquire_job`
eligibility, restricted to offsite / non-LinkedIn). Run on the home box against `applypilot.db`:

```sql
SELECT
    url,
    company,
    title,
    application_url,
    CAST(COALESCE(audit_score, fit_score) AS REAL) AS score
FROM jobs
WHERE duplicate_of_url IS NULL
  AND COALESCE(audit_score, fit_score) >= 7
  AND COALESCE(liveness_status, '') != 'dead'
  AND (apply_status IS NULL OR apply_status NOT IN ('applied', 'in_progress'))
  -- offsite ATS only: a real http(s) apply target, never LinkedIn (no cookies offsite)
  AND application_url LIKE 'http%'
  AND application_url NOT LIKE '%linkedin.com%'
  -- belt-and-suspenders: exclude rows whose apply target is already applied/in-flight
  -- under posting-level dedup (so the queue never carries a known double-submit)
  AND COALESCE(application_url, url) NOT IN (
        SELECT COALESCE(application_url, url) FROM jobs
        WHERE apply_status IN ('applied', 'in_progress')
           OR apply_error IN ('no_confirmation', 'crash_unconfirmed')
  )
ORDER BY score DESC;
```

~2,499 offsite-eligible rows at design time. The Python pusher must additionally run each
candidate through `config.is_auth_gated_application` / `is_unresolved_aggregator` /
`is_manual_ats` (host logic, not SQL-expressible) and skip those, plus compute `apply_domain`
using the same `_apply_target` / `_throttle_host` logic (`launcher.py:1616-1635`).

**PUSH Step B — idempotent UPSERT into Postgres by `url`** (re-runnable; never disturbs a
leased/terminal row):

```sql
INSERT INTO apply_queue (url, company, title, application_url, score, apply_domain, status)
VALUES ($1, $2, $3, $4, $5, $6, 'queued')
ON CONFLICT (url) DO UPDATE
SET company         = EXCLUDED.company,
    title           = EXCLUDED.title,
    application_url  = EXCLUDED.application_url,
    score           = EXCLUDED.score,
    apply_domain    = EXCLUDED.apply_domain,
    updated_at      = now()
WHERE apply_queue.status = 'queued';   -- only refresh+requeue still-pending rows
```

**PULL Step A — fetch terminal, not-yet-ingested results from Postgres:**

```sql
SELECT url, status, apply_status, apply_error, verification_confidence,
       est_cost_usd, applied_at, worker_id, apply_duration_ms
FROM apply_queue
WHERE status IN ('applied', 'failed', 'blocked', 'crash_unconfirmed')
  AND synced_to_home_at IS NULL
ORDER BY updated_at
LIMIT 500;
```

**PULL Step B — map each result back into the home `jobs` row, idempotently** (SQLite):

```sql
-- applied: confirm submit, clear in-flight markers (idempotent via COALESCE + guard).
UPDATE jobs
SET apply_status            = 'applied',
    applied_at              = COALESCE(:applied_at, applied_at),
    apply_error             = NULL,
    agent_id                = NULL,
    verification_confidence = :verification_confidence,
    apply_duration_ms       = :apply_duration_ms
WHERE url = :url
  AND COALESCE(apply_status, '') != 'applied';
```

```sql
-- failed / blocked / crash_unconfirmed: pin attempts so the home loop won't re-acquire.
UPDATE jobs
SET apply_status      = CASE WHEN :status = 'blocked' THEN 'failed' ELSE :status END,
    apply_error       = :apply_error,
    apply_attempts    = 99,             -- mirrors reclaim_stale_leases / permanent-failure pin
    agent_id          = NULL,
    apply_duration_ms = :apply_duration_ms
WHERE url = :url
  AND COALESCE(apply_status, '') != 'applied';   -- never demote a confirmed apply
```

After each home write succeeds, stamp the Postgres row ingested (makes PULL idempotent):

```sql
UPDATE apply_queue SET synced_to_home_at = now() WHERE url = $1;
```

Optionally also `record_application(url, status=..., channel='offsite_fleet', update_job=False)`
and append to `llm_usage` (`stage='apply_agent'`, `est_cost_usd`) so home's durable cost
accounting picks up fleet spend exactly as the local path does (same wiring as `run_job`,
`launcher.py:1252-1277`).

### 3c. Linux worker container — reuse map, minimal loop, what's stripped

**REUSE verbatim (host/OS-agnostic, zero changes):**
- `build_apply_agent_command(agent="claude", model="sonnet", mcp_config_path, cdp_port)` —
  `launcher.py:185-224`. Emits `-p --mcp-config --permission-mode bypassPermissions
  --no-session-persistence --disallowedTools ... --output-format stream-json --verbose -`.
- `_make_mcp_config(cdp_port)` — `launcher.py:101-123`. Pins `@playwright/mcp@0.0.76`,
  `--cdp-endpoint=http://localhost:<port>`. Gmail MCP stays OFF
  (`APPLYPILOT_ENABLE_GMAIL_MCP` unset).
- stream-json parser `_consume_stream` — `launcher.py:1098-1191`. Reads `result` message;
  `cost_usd = msg.get("total_cost_usd", 0)` (`:1144-1152`) is the **real billed dollars**.
- result classifier — `launcher.py:1289-1319` (`RESULT:APPLIED/EXPIRED/CAPTCHA/LOGIN_ISSUE/
  AUTH_REQUIRED/FAILED:reason/DRY_RUN`) + `_is_auth_required_result` (`:1377-1383`).
- cost persistence — `record_llm_usage(stage="apply_agent", ...)` (`launcher.py:1252-1277`,
  insert `database.py:666-705`). NOTE: today this only fires when `cost` is truthy
  (`launcher.py:1263`); in the fleet, write the `apply_queue` result row UNCONDITIONALLY with
  `est_cost_usd = 0` when absent, so the cap `SUM` and the lease ledger stay consistent.
- env hygiene — `env = os.environ.copy()` then `env.pop("CLAUDECODE", None)` /
  `env.pop("CLAUDE_CODE_ENTRYPOINT", None)` before `subprocess.Popen` (`launcher.py:1054-1056`).
  KEEP — a Railway container started by a parent agent could otherwise inherit nested-session mode.
- CDP-readiness poll — `chrome.py:524-542`. KEEP (prevents the agent connecting to a
  not-yet-ready port and dying `no_result`).
- crash safety — port `reclaim_stale_leases` (`launcher.py:861-892`) to Postgres (§5); call on
  worker startup AND periodically (home pattern: `supervisor.py:198`).

**STRIP / REPLACE (Windows / home-only):**
- `setup_worker_profile` profile-cloning + LinkedIn seed (`chrome.py:228-294`) — DROP. Use a
  throwaway empty `--user-data-dir` per lease.
- `linkedin_login` (`chrome.py:375-423`) — DROP entirely (no cookies offsite).
- Windows Job-Object kill-on-close `_assign_kill_on_close_job` / `_close_*` (`chrome.py:111-209`)
  — no-op on Linux (already guarded `if platform.system() != "Windows": return`).
- `taskkill /F /T` branch (`chrome.py:59-63`) — never reached on Linux; Unix `os.killpg` /
  `os.setsid` path (`chrome.py:68`, `503-509`) is used.
- `_suppress_restore_nag` Preferences.json patch (`chrome.py:426-446`) — DROP.
- `mirror_db_offsite` OneDrive mirror (`launcher.py:803-848`) — DROP (Postgres is authoritative).
- Local SQLite paths (`config.py:19-78`) — replace with PG connection; `CHROME_WORKER_DIR`/
  `APPLY_WORKER_DIR` -> `/tmp/...` (ephemeral). `get_claude_path` (`config.py:129-157`) ->
  `CLAUDE_PATH=/usr/local/bin/claude`. `CHROME_PATH=/usr/bin/google-chrome` (or Playwright's
  bundled Chromium).
- `launch_chrome` flags (`chrome.py:453-542`): use `--headless=new` (`:503`) PLUS add
  `--no-sandbox` and `--disable-dev-shm-usage` (root container; small `/dev/shm`).
  `BASE_CDP_PORT=9222` (`chrome.py:20-21`); one worker per container -> single fixed port.

**Minimal worker loop (pseudocode):**

```python
# applypilot/apply/container_worker.py
from applypilot.apply.launcher import (
    build_apply_agent_command, _make_mcp_config, run_job, _throttle_after_apply,
)
from applypilot import config
from . import pgqueue  # NEW: psycopg layer holding the SQL in §3a/§5/§6

def container_worker_main(worker_id: str, model: str = "sonnet"):
    config.load_env()
    pgqueue.reclaim_stale_leases(grace_seconds=30)     # startup crash sweep
    port = config.BASE_CDP_PORT                          # 9222, one worker/container

    while True:
        if pgqueue.should_halt():                        # §6: paused OR SUM>=cap
            break
        job = pgqueue.lease_one(worker_id=worker_id, ttl_seconds=1200)  # §5 SKIP LOCKED, host-polite
        if not job:
            time.sleep(POLL_INTERVAL); continue

        proc = launch_chrome_headless(port)              # --no-sandbox --disable-dev-shm-usage --headless=new
        try:
            status, duration_ms, cost = run_job(         # REUSED agent + parser + classifier
                job=job, port=port, worker_id=worker_id, model=model, agent="claude")
        finally:
            cleanup_worker(proc)                         # os.killpg, rm /tmp user-data-dir

        pgqueue.write_result(worker_id, job["url"], status, duration_ms, cost)  # §5, lease_owner-guarded, unconditional
        _throttle_after_apply(job["application_url"])    # 15-40s jittered inter-job delay
```

### 3d. Dockerfile outline

```dockerfile
# Pin the Playwright base to the version matching @playwright/mcp@0.0.76's bundled browser.
FROM mcr.microsoft.com/playwright:v1.52.0-noble

# Node + npx ship in the Playwright image. Pre-install the MCP + Claude CLI globally
# so the first lease pays no cold npx download.
RUN npm i -g @playwright/mcp@0.0.76 @anthropic-ai/claude-code

# Python worker (slim): reuse launcher.py / chrome.py / config.py + a psycopg pgqueue layer.
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*
COPY . /app
WORKDIR /app
RUN pip3 install --no-cache-dir -e . psycopg[binary]

ENV CLAUDE_PATH=/usr/local/bin/claude \
    CHROME_PATH=/ms-playwright/chromium-*/chrome-linux/chrome \
    APPLYPILOT_DIR=/data/applypilot \
    CHROME_WORKER_DIR=/tmp/chrome-workers \
    APPLY_WORKER_DIR=/tmp/apply-workers \
    APPLYPILOT_LANE_FILTER=1 \
    APPLYPILOT_PREFLIGHT_LIVENESS=0
# NOT baked into the image (set in Railway, sealed): the metered model key + DATABASE_URL.
#   Provider via MODEL_PROVIDER (§3e): anthropic -> ANTHROPIC_API_KEY; deepseek -> ANTHROPIC_API_KEY
#   (DeepSeek key) + ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic.
# PII (profile.json, resume.pdf) is mounted from a Railway volume at /data/applypilot,
#   NEVER baked into the image. config reads APPLYPILOT_PROFILE_PATH / APPLYPILOT_RESUME_PDF_PATH
#   (config.py:10,20-22); --base-resume default falls back to RESUME_PDF_PATH (config.py:50-60),
#   so one base resume.pdf on the volume is sufficient.

CMD ["python3", "-m", "applypilot.apply.container_worker"]
```

**Secret / PII delivery:** `ANTHROPIC_API_KEY` and `DATABASE_URL` set in the Railway dashboard
(API key **sealed**). `profile.json` + base `resume.pdf` live on a Railway **volume** mounted at
`/data/applypilot`. The image is PII-free and API-key-free; registry pushes never carry
identity or billing credentials. Do NOT mount the home `~/.claude` / `~/.applypilot` OAuth into
the image — a clean container with only `ANTHROPIC_API_KEY` guarantees metered billing (no
subscription credential can take precedence); do not set `ANTHROPIC_AUTH_TOKEN`.

### 3e. Model provider — Sonnet vs DeepSeek (decided by the POC A/B)

The hard requirement is a **metered API key**, NOT Anthropic specifically — a subscription
can't power a fleet; a pay-per-token key can, and that key can be DeepSeek. The provider is left
as a **per-worker env choice resolved by the POC**, because the right metric for the apply agent
is **$/successful-apply + application quality**, not $/token:

- **Cost:** DeepSeek is roughly ~10x cheaper per token than Sonnet. If quality holds, the full
  offsite run drops from ~$2-3k to **~$200-400**. That is the prize, and the reason to test it.
- **Risk:** applying is hard, long-horizon **agentic browser work** whose output is
  **irreversible and submitted under Jonathan's real name** at real employers. A weaker model
  that mis-fills a field, loops/retries, or — worst — submits a *fabricated* answer costs far
  more than the tokens it saved. Frontier models still tend to lead at multi-step tool-use
  reliability; whether DeepSeek closes that gap on real ATS forms is an empirical question, not
  an assumption — so we measure it (§7) rather than guess.
- **PII flag:** the agent sends profile / resume / work-history + the employer page to whichever
  provider backs it. DeepSeek is a different jurisdiction and data policy than Anthropic — worth
  knowing; not a blocker.

**Wiring (one image; provider chosen by env `MODEL_PROVIDER` ∈ {anthropic, deepseek}):**
- `anthropic`: `ANTHROPIC_API_KEY=<sk-ant…>`, model `sonnet`, agent `claude` (as specced).
- `deepseek` via the Claude Code CLI: point it at DeepSeek's Anthropic-compatible endpoint —
  `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`, `ANTHROPIC_API_KEY=<deepseek key>`,
  model `deepseek-chat` (or `deepseek-reasoner`). Fallback path: the already-supported **Codex**
  backend (`build_apply_agent_command(agent="codex")`, `launcher.py:226-237`) with a DeepSeek
  provider config. P1 verifies which path drives the Playwright MCP loop most reliably.
- The worker already takes a `model` arg (§3c); `MODEL_PROVIDER` selects base-URL + key env +
  model + agent backend. Each result is tagged with `agent_model` (§3a) so §7 compares providers.

**Decision = the POC (§7):** run an A/B and pick the provider with the **lowest
$/successful-apply that clears the quality bar** (incl. a manual spot-check for fabrication). The
spec's `model="sonnet"` default holds only until the A/B says otherwise.

---

## 4. Data flow

```
1. HOME: resolver/scorer/enrichment write the full 60-col job into SQLite jobs (UNCHANGED).
2. HOME PUSH: SELECT offsite-eligible (score>=7, non-LinkedIn, http%, not applied/in-flight,
   not auth-gated/aggregator/manual-ats) -> UPSERT ~6 cols into apply_queue (status='queued').
3. PG QUEUE: rows sit 'queued', ordered by score DESC.
4. FLEET: each worker checks fleet_config, leases the top host-polite 'queued' row
   (FOR UPDATE SKIP LOCKED), flips it 'leased' with lease_owner/lease_expires_at, bumps attempts.
5. APPLY: worker launches headless Chromium -> runs the EXISTING claude apply agent over the
   Playwright MCP against application_url -> stream-json yields RESULT:<status> + total_cost_usd.
6. RESULT: worker UPDATEs the row (status applied|failed|blocked|crash_unconfirmed,
   apply_error, est_cost_usd, applied_at, worker_id, duration), guarded by lease_owner.
7. HOME PULL: SELECT terminal+unsynced rows -> map into jobs (applied / failed+attempts=99 /
   crash_unconfirmed) -> stamp synced_to_home_at -> optionally append llm_usage + applications.
8. Home loop never re-acquires a fleet-decided posting (apply_attempts=99 + posting-level dedup).
```

Cross-wire payload OUT (~6 cols): `url, company, title, application_url, score, apply_domain`.
Payload BACK: `url, status, apply_status, apply_error, est_cost_usd, applied_at, worker_id,
apply_duration_ms, verification_confidence`. The 60-col schema, user profile, and local file
paths NEVER cross.

---

## 5. Anti-double-submit & lease / reclaim

**Lease (atomic, single round-trip)** — `$1`=worker_id, `$2`=lease TTL seconds (must exceed the
agent wall-clock timeout; home rule `STALE_LEASE_SECONDS = max(AGENT_TIMEOUT+120, 1200)`).
Host-polite variant excludes domains touched inside the jittered politeness window
(generalizes `_throttle_host` / `_linkedin_gap_wait`, `launcher.py:1573-1594, 1616-1661`):

```sql
WITH host_recent AS (
    SELECT apply_domain, MAX(last_attempted_at) AS last_at
    FROM apply_queue
    WHERE last_attempted_at > now() - interval '90 seconds'
    GROUP BY apply_domain
),
next_job AS (
    SELECT q.url
    FROM apply_queue q
    LEFT JOIN host_recent hr ON hr.apply_domain = q.apply_domain
    WHERE q.status = 'queued'
      AND (hr.last_at IS NULL
           OR hr.last_at < now() - (interval '90 seconds' * (0.7 + random()*0.7)))
    ORDER BY q.score DESC, q.url            -- url tie-break = deterministic, mirrors home
    LIMIT 1
    FOR UPDATE OF q SKIP LOCKED             -- N workers each grab a distinct row, no blocking
)
UPDATE apply_queue q
SET status            = 'leased',
    lease_owner       = $1,
    lease_expires_at  = now() + ($2 || ' seconds')::interval,
    last_attempted_at = now(),
    attempts          = q.attempts + 1,
    updated_at        = now()
FROM next_job
WHERE q.url = next_job.url
RETURNING q.url, q.company, q.title, q.application_url, q.score, q.apply_domain, q.attempts;
```

`attempts` is bumped **at lease time**, in the same statement, so "did this row ever launch?"
is observable to reclaim even if the worker dies before writing a result.

**Reclaim stale leases** (startup + periodic) — `$1`=grace seconds. A leased row past
`lease_expires_at` means a hard crash (a clean finish always writes terminal status). Only the
pre-launch window (`attempts <= 1` AND no `apply_error`) is safe to requeue; everything else may
have clicked submit -> park `crash_unconfirmed`, pin `attempts=99`, NEVER re-lease (mirrors
`reclaim_stale_leases`, `launcher.py:861-892`, `apply_attempts=99`):

```sql
WITH stale AS (
    SELECT url, attempts, apply_error
    FROM apply_queue
    WHERE status = 'leased'
      AND lease_expires_at < now() - ($1 || ' seconds')::interval
    FOR UPDATE SKIP LOCKED
)
UPDATE apply_queue q
SET status = CASE
        WHEN s.attempts <= 1 AND s.apply_error IS NULL THEN 'queued'::apply_queue_status
        ELSE 'crash_unconfirmed'::apply_queue_status
    END,
    apply_error = CASE
        WHEN s.attempts <= 1 AND s.apply_error IS NULL THEN NULL
        ELSE 'crash_unconfirmed'
    END,
    attempts = CASE
        WHEN s.attempts <= 1 AND s.apply_error IS NULL THEN s.attempts
        ELSE 99
    END,
    lease_owner      = NULL,
    lease_expires_at = NULL,
    updated_at       = now()
FROM stale s
WHERE q.url = s.url
RETURNING q.url, q.status;
```

**Terminal result write** (releases lease; `lease_owner`-guarded so a reclaimed row is never
clobbered):

```sql
UPDATE apply_queue
SET status                  = $2,    -- applied|failed|blocked|crash_unconfirmed
    apply_status            = $3,
    apply_error             = $4,
    verification_confidence = $5,
    est_cost_usd            = $6,     -- write UNCONDITIONALLY (0 when CLI reports none)
    applied_at              = CASE WHEN $2 = 'applied' THEN now() ELSE applied_at END,
    worker_id               = $7,
    apply_duration_ms       = $8,
    lease_owner             = NULL,
    lease_expires_at        = NULL,
    updated_at              = now()
WHERE url = $1
  AND lease_owner = $7;     -- only the lease holder may close it
```

**Defense in depth (3 layers):** (1) `FOR UPDATE SKIP LOCKED` row lock — no two live workers
grab the same row; (2) lease-expiry + `crash_unconfirmed` — a possibly-submitted crashed job is
never re-offered; (3) home posting-level dedup on PULL (`apply_attempts=99` +
`COALESCE(application_url,url)` dedup, `acquire_job` ~576-604) — even a re-pushed sibling URL of
a submitted posting is suppressed.

---

## 6. Cost controls & global spend cap

Per-job cost is the CLI's own `total_cost_usd` from the stream-json `result` message
(`launcher.py:1144-1152`) — the **real metered dollar amount**, not a token estimate
(`_estimate_cost` in `llm.py:491-500` is the scoring fallback only; ignore it here). It is
written to `apply_queue.est_cost_usd` co-located with `status` so the cap is a single-table SUM.
**The provider choice swings this ~10x** (§3e): the full offsite run is ~$2-3k on Sonnet vs
~$200-400 on DeepSeek — set `spend_cap_usd` for whichever provider the POC selects.

**Pre-lease gate** (evaluated before/with each lease, ideally same connection):

```sql
SELECT
    fc.paused
    OR (fc.spend_cap_usd > 0
        AND COALESCE((SELECT SUM(est_cost_usd) FROM apply_queue), 0) >= fc.spend_cap_usd)
        AS should_halt
FROM fleet_config fc
WHERE fc.id = 1;
```

If `should_halt`, the worker stops leasing and exits/idles. This ports the home supervisor
semantics (`_apply_cost_total` `supervisor.py:46-63`; stop loop `:187-191, 242-243`) to
Postgres + `fleet_config.spend_cap_usd`.

**Soft vs hard cap:** the gate is a soft ceiling checked before each lease, so N workers can
overshoot by up to ~N in-flight jobs (~$1.50 each) — matching the home supervisor's slack
(`supervisor.py:188`). For the POC's ~$2-3k budget this overshoot is irrelevant. For a hard cap,
re-check the SUM inside the lease transaction and abort if exceeded. Always write the result
row (including `est_cost_usd=0`) so cap math stays consistent.

**Railway compute budget (secondary):** ~$0.0695/worker-hr at 1.5 vCPU / 2 GB. POC (3 workers,
~6h) ~= $1.25; 5 workers/3 days ~= $25; 10 workers/3 days ~= $50. Plan subscription absorbs the
first $5 (Hobby) / $20 (Pro). Anthropic API (~$2-3k) dominates.

---

## 7. POC gate — exact metrics & pass/fail threshold

Run **2-3 workers** on plain Railway datacenter IPs (Hobby). This gate does **double duty** —
it validates datacenter-IP viability AND runs the **Sonnet-vs-DeepSeek A/B** (§3e). Target
**~150 applies split ~evenly across the two providers** (tag each via `agent_model`), then
query Postgres for the metrics below — overall AND broken down by provider:

| Metric | Definition (SQL over apply_queue) | Target |
|---|---|---|
| attempt-success % | `applied / (applied + failed + blocked + crash_unconfirmed)` | **>= ~36%** (home baseline), i.e. within ~5pp |
| captcha/block % | `(blocked + apply_error ILIKE '%captcha%') / attempts` | low single digits; a spike => IP-reputation problem |
| $/apply | `SUM(est_cost_usd) / COUNT(status='applied')` | sane vs home (order ~$1.50/apply); flag if >2x |
| crash_unconfirmed % | `crash_unconfirmed / attempts` | near 0; >2-3% => stability/timeout problem |

Metrics query:

```sql
SELECT
    COUNT(*) FILTER (WHERE status='applied')                               AS applied,
    COUNT(*) FILTER (WHERE status IN ('applied','failed','blocked','crash_unconfirmed')) AS attempted,
    ROUND(100.0 * COUNT(*) FILTER (WHERE status='applied')
          / NULLIF(COUNT(*) FILTER (WHERE status IN ('applied','failed','blocked','crash_unconfirmed')),0), 1) AS success_pct,
    COUNT(*) FILTER (WHERE status='blocked' OR apply_error ILIKE '%captcha%') AS blocked_or_captcha,
    COUNT(*) FILTER (WHERE status='crash_unconfirmed')                     AS crash_unconfirmed,
    COALESCE(SUM(est_cost_usd),0)                                          AS total_cost,
    ROUND(COALESCE(SUM(est_cost_usd),0) / NULLIF(COUNT(*) FILTER (WHERE status='applied'),0), 4) AS cost_per_apply
FROM apply_queue;
```

**PASS** (proceed to scale-out): success% within ~5pp of the home ~36% baseline AND
captcha/block% in the low single digits AND $/apply within ~2x of home AND crash_unconfirmed%
near 0.

**FAIL / iterate:** success% materially below ~31% OR a captcha/block spike (datacenter-IP
reputation) -> go to §8 (Static Outbound IPs, then residential proxy) and re-run the gate
before spending the budget at scale. Do NOT scale past the gate.

**Model A/B decision (§3e):** break the metrics down by `agent_model` and pick the provider with
the **lowest $/successful-apply that still clears the success bar**. Automated success% is NOT
sufficient for the cheaper model — **manually spot-check ~10-15 DeepSeek `applied` rows** for
mis-filled fields or fabricated answers before trusting it, because a confidently-wrong
submission counts as "applied" yet is the exact failure that would justify paying for Sonnet.

---

## 8. Egress / IP + residential-proxy fallback

- **Default:** Railway egress leaves from a shared cluster IP that changes on deploy/restart/
  scale. Shared datacenter ranges can carry poor reputation -> third-party blocks. This is
  exactly what the POC gate measures.
- **First escalation (cheapest, Pro-only):** **Static Outbound IPs** — service Settings ->
  Networking -> "Enable Static IPs" -> redeploy. Egress over ~3 permanent IPv4s. Caveat: may be
  shared with other Railway customers (stabilizes the IP for allowlists; does NOT guarantee
  clean/residential reputation). Requires Pro.
- **Second escalation (true dedicated/residential):** route the worker's offsite egress through
  an external proxy via `HTTP_PROXY`/`HTTPS_PROXY` env or Playwright's `proxy` launch option.
  Cheapest dedicated static egress: QuotaGuard / Fixie. True residential rotation: Bright Data /
  Oxylabs / Smartproxy-class, plugged in the same way.
- **Gating rule:** add proxies ONLY if the POC shows offsite blocks on plain datacenter IPs.
  Keep this out of the build until the gate says so.

---

## 9. Error handling

| Failure | Behavior | Mechanism |
|---|---|---|
| Chrome launch fails | mark `failed` (permanent), release lease, next job | try/except around `launch_chrome` in the loop |
| Agent stdout hangs | kill process after `AGENT_TIMEOUT_SECONDS` (~900s), `failed:timeout` | reader-thread timeout (`_consume_stream`) |
| No `RESULT:` line | `failed:no_result_line` (permanent) | classifier `launcher.py:1289-1319` |
| API key exhausted mid-job | agent errors in stream-json -> `failed:auth_required`-class; cap still enforced durably | result parser + §6 SUM gate |
| Worker hard-crashes mid-apply | lease expires -> `crash_unconfirmed`, never re-leased | §5 reclaim |
| Postgres unreachable | `lease_one` raises -> worker sleeps + retries; home is source of truth | loop try/except + backoff |
| CLI emits `total_cost_usd: 0` | write result row with `est_cost_usd=0` (do NOT skip) | unconditional result write (§5) |
| Captcha / Cloudflare wall | `blocked` (terminal); feeds POC captcha% + §8 decision | classifier RESULT:CAPTCHA / block reasons |
| Stale posting expired on visit | `failed:expired`; consider `APPLYPILOT_MAX_JOB_AGE_DAYS` push filter | classifier RESULT:EXPIRED |

PULL maps `blocked` -> home `apply_status='failed'` with `apply_attempts=99`; `crash_unconfirmed`
carries home to drive posting-level dedup. Never demote a confirmed `applied` (guard
`COALESCE(apply_status,'') != 'applied'`).

---

## 10. Testing

**Unit (pure SQL / psycopg against a local Postgres):**
- *Lease atomicity under concurrency:* fire K parallel `lease_one` against M queued rows;
  assert each row leased by exactly one worker, no double-grab, count(distinct url)=min(K,M).
- *Reclaim vs crash_unconfirmed:* seed leased+expired rows with `attempts=1/no-error` (=> requeued
  `queued`) and `attempts>=2`-or-`apply_error` (=> `crash_unconfirmed`, `attempts=99`); assert the
  unconfirmed row is never returned by a subsequent `lease_one`.
- *Cost-cap halt:* set `spend_cap_usd`, insert `est_cost_usd` rows summing to/over the cap;
  assert `should_halt` flips and `lease_one` returns nothing; also test `paused=TRUE`.
- *Sync idempotency:* run PUSH twice (assert no duplicate/reopened rows; leased/terminal
  untouched); run PULL twice (assert second pass is a no-op via `synced_to_home_at`); assert
  `applied` is never demoted.
- *lease_owner guard:* a result write from a non-holder (post-reclaim) no-ops.

**Integration (docker-compose):** Postgres + 2 worker containers + a fake ATS page server.
Run end-to-end: PUSH a handful of fake jobs, let workers lease/apply against the fake form,
PULL results, assert home-side jobs rows updated correctly and cost summed. Verify
`@playwright/mcp@0.0.76` + headless Chromium (`--no-sandbox --disable-dev-shm-usage`) launches
and the agent reaches `RESULT:`.

**Live POC:** the §7 gate — 2-3 workers, ~100 real offsite applies, datacenter IPs, then the
metrics query and pass/fail decision.

---

## 11. Build phases / milestones

- **P0 — Schema + sync (home-side, no cloud):** apply_queue + fleet_config DDL; psycopg
  `pgqueue` layer (lease/reclaim/should_halt/write_result); home PUSH/PULL scripts. Unit tests
  green against local Postgres.
- **P1 — Worker container:** `container_worker.py` reusing launcher/chrome; Dockerfile;
  strip Windows/home bits; docker-compose integration test (fake ATS) green.
- **P2 — Railway wiring (needs Jonathan's account steps, §12):** `railway init/up`, Postgres
  add, `DATABASE_URL` reference var, sealed `ANTHROPIC_API_KEY`, volume with profile.json +
  resume.pdf. Single worker smoke test (1-2 real applies).
- **P3 — POC gate:** scale to 2-3 replicas, run ~100 applies, query metrics, pass/fail (§7).
- **P4 — Egress remediation (only if P3 fails on blocks):** Static Outbound IPs (Pro) ->
  proxy fallback (§8); re-run gate.
- **P5 — Scale-out (only past gate):** 5 workers on Hobby (<=6 replicas) or Pro for >6; monitor
  spend cap; iterate.

---

## 12. RUNBOOK

### Steps only Jonathan can do (account-owner)
1. **Sign up** at `railway.com` (GitHub OAuth or email); verify email.
2. **Pick plan + add card:** Account/Workspace Settings -> Plans/Billing -> choose **Hobby
   ($5/mo)** for the POC (covers <=6 replicas; fine for 3 and 5 workers). Upgrade to **Pro
   ($20/mo)** only for >6 replicas OR to enable Static Outbound IPs. Add a payment card.
3. **Generate the metered key(s) for the POC A/B (§3e)** — an **Anthropic** API-billed key
   (`sk-ant…`, NOT the Claude subscription) and a **DeepSeek** key — and **paste into Railway** as
   service variables, then **Seal** them (write-only; irreversible; not returned by CLI/API).
4. **One-time CLI auth:** install the Railway CLI, run `railway login` (browser OAuth;
   `railway login --browserless` for a headless box). After this, Claude can script
   `railway init/link/up` from the home box.

### Steps Claude drives
1. Write all code: `pgqueue` psycopg layer (the §3a/§5/§6 SQL), home PUSH/PULL scripts
   (§3b), `container_worker.py` (§3c), the Dockerfile (§3d). No application-agent rewrites —
   reuse `build_apply_agent_command` / `_make_mcp_config` / parser / classifier.
2. Author + run the schema migration (apply_queue, fleet_config, indexes, seed `fleet_config`
   row, `UPDATE fleet_config SET spend_cap_usd=<budget>`).
3. `railway init` / `railway link`; `railway add` Postgres; set the worker's
   `DATABASE_URL=${{Postgres.DATABASE_URL}}` (private) reference var and home-box scripts use
   `${{Postgres.DATABASE_PUBLIC_URL}}`.
4. Set all **non-secret** vars (`APPLYPILOT_DIR=/data/applypilot`, `CHROME_WORKER_DIR=/tmp/...`,
   `APPLYPILOT_LANE_FILTER=1`, `APPLYPILOT_PREFLIGHT_LIVENESS=0`, `CLAUDE_PATH`, `CHROME_PATH`,
   `APPLYPILOT_MAX_JOB_AGE_DAYS=14`). (Jonathan seals the API key separately.)
5. `railway up` (build + deploy the Dockerfile). Smoke-test one worker on 1-2 real applies.
6. Run PUSH from the home box; scale to 2-3 replicas via Settings -> Scale dial.
7. Run the **POC gate** (§7): let ~100 applies complete, run the metrics SQL against Postgres,
   compute pass/fail vs the ~36% home baseline.
8. Iterate: if blocked, add Static Outbound IPs (after Jonathan upgrades to Pro) or a proxy
   (§8), re-run the gate.
9. Run PULL to ingest results into home SQLite; reconcile cost into home `llm_usage`.
10. On completion: pause the fleet (`UPDATE fleet_config SET paused=TRUE`), scale replicas to 0,
    `railway down` / teardown to stop compute billing.

---

## Open items flagged by research (not blockers, noted for implementation)
- Replica count is **dashboard-only** (Settings -> Scale dial); no documented
  `railway.json`/CLI key — Claude sets it in the UI or asks Jonathan to.
- Hobby per-replica vCPU/RAM cap isn't separately documented (only the 48 vCPU / 48 GB
  aggregate); 5 workers at 1.5 vCPU / 2 GB = 7.5/10, well within it.
- `verification_confidence` is 100% NULL in the live home DB (`mark_result` never populates it)
  — treat as a pass-through the offsite agent may begin emitting; do NOT gate on it.
- Pin the Playwright base image tag to the browser version bundled with
  `@playwright/mcp@0.0.76` (outline uses `v1.52.0-noble` as a placeholder — verify at build).
