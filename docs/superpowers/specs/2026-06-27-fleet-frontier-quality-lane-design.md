# Fleet Frontier Quality Lane — Design Spec

**Date:** 2026-06-27 (rev 2 — subscription-priority)
**Status:** design, pending review
**Repo:** `New project/ApplyPilot` (Python tool)
**Depends on:** the shipped **compute lane** (`scoring/scorer.py`, `fleet/compute_adapters.py`,
123 PG tests). Grounding: memory `applypilot-subscription-scoring` (ToS verdicts: Codex YELLOW,
Claude Max stricter, Gemini excluded).
See [`2026-06-27-fleet-compute-lane-design.md`](2026-06-27-fleet-compute-lane-design.md).

## 1. Goal & success criteria

Put **frontier-model judgment as the PRIORITY for the non-bulk scoring work** — everything beyond
the cheap first pass DeepSeek does — using the user's own **top-tier subscriptions** (Codex **Pro**
/ gpt‑5.5 as the primary; Claude **Max** / Opus as an optional attended cross-check), paced under
their caps and failing over to a metered frontier API so the lane never stalls. The user's goal: the
best reasoning models on the jobs that matter, paid from sunk flat-rate quota, with the cheap API as
the floor, not the ceiling.

**Backend priority (per job, in order):**
1. **Codex subscription (gpt‑5.5)** — PRIMARY frontier backend, via `codex exec` on the user's
   logged-in home box. Rate-governed, serial + jitter.
2. **Metered frontier API** (e.g. `gpt-5.5`/`o`-class or `claude-opus` via API key) — **failover**
   when the subscription is capped/unavailable, and the **default** backend when the subscription
   toggle is off. ToS-clean.
3. **Claude Max (Opus)** — OPTIONAL, default-OFF, **attended cross-check** on the highest-value
   subset only (not an unattended engine — Anthropic's Consumer Terms are stricter on this pattern).
Cheap **DeepSeek** stays the separate, unchanged **bulk** lane (the shipped compute lane).

**Done when:** `applypilot-fleet-frontier` works down the priority-ordered backlog of not-yet-
frontier-scored jobs, scoring each with the Codex subscription backend (rate-governed), recording the
frontier score + agreement-vs-cheap in a self-contained `frontier_scores` brain side-table (advisory
— the `jobs` table is NOT migrated), failing over to the metered API when the sub caps, and printing
a disagreement report. Verified by tests with stubbed backends (no spend / no real CLI); opt-in live
smokes for the subscription (`codex exec`), the metered API, and the optional Claude cross-check.

**Non-goals:** changing the bulk compute lane; cover-letter / diagnosis re-routing (a later adopter
of the same backend — those touch in-flight files); resume tailoring (the user's rule); Gemini (login
path removed 2026‑06‑18); **pooling anyone else's subscription** (a hard ToS line).

## 2. Flow

```
Brain (SQLite jobs)  ── cheap research_fit_score written by the BULK compute lane (DeepSeek) ──┐
        │                                                                                       │
        ▼  select_priority(backlog)  (highest-value not-yet-frontier-scored, ordered)           │
  per job, SERIAL, gap-jitter:                                                                   │
     governor.allow(account) ? ──yes──► Codex subscription (codex exec, gpt-5.5)                 │
                              └─no/limit─► metered frontier API (failover)                       │
     [optional, attended] also score on Claude Max (Opus) for the top tier → cross-check         │
        │                                                                                        │
        ▼  agreement = f(cheap_score, frontier_score[, opus_score])                              │
  frontier_scores side-table (url, cheap, frontier, opus?, provider, agreement, scored_at) ◄─────┘
        │
        ▼  disagreement_report  ──►  owner-review queue (low-agreement jobs)
```

Advisory throughout; `jobs.fit_score`/`audit_score` never touched.

## 3. Components

### 3.1 `fleet/frontier_select.py` — priority backlog selector
`select_priority(sqlite_conn, *, limit, floor=7.0, mode="backlog", hours=24, urls=None) -> list[dict]`
(`{url, company, title, full_description, cheap_score}`). Modes:
- **`backlog` (default):** not-yet-frontier-scored jobs (no `frontier_scores` row), `duplicate_of_url
  IS NULL`, `COALESCE(research_fit_score, fit_score) >= floor`, **ordered by cheap_score desc** (best
  first), `LIMIT limit`. The lane works down this priority queue across runs as quota allows.
- **`new`:** discovered within the last `hours` (the daily trickle). **`urls`:** an explicit set.
`floor`/`limit` are arguments. (Default `floor=7.0` so the frontier judge is spent on plausible jobs,
not obvious rejects; set lower to cover more.)

### 3.2 `fleet/cli_providers.py` — subscription-CLI backends
- `score_via_codex(prompt, *, schema_path, model=None, timeout_s=120, retries=2) -> dict`: runs
  `codex exec [-m <model>] --output-schema <schema_path> -o <tmp.json> "<prompt>"`, parses
  `<tmp.json>` → `{"score", "reasoning", ...}`; bounded retry on malformed; raises
  **`SubscriptionUnavailable`** on non-zero exit / auth / **quota/limit** / parse-exhaustion. Uses
  `--output-schema`/`-o` (single object), never `--json` (event stream). The `model` lets the caller
  pick the **Codex model per tier** (gpt‑5.5 for the top tier; a smaller/faster Codex model — which
  has *higher* per‑window caps — for the broader backlog, stretching subscription coverage). The
  exact `-m`/`--output-schema` flags are verified against `codex exec --help` at build time.
- `score_via_claude(prompt, *, schema, timeout_s=120) -> dict` (OPTIONAL, used only by the attended
  cross-check): `claude -p --output-format json --json-schema '<schema>'`, parse `.structured_output`;
  requires `CLAUDE_CODE_OAUTH_TOKEN` set and `ANTHROPIC_API_KEY` unset and **not** `--bare` (else it
  bills the metered API); raises `SubscriptionUnavailable` on limit/auth/parse failure.
These modules shell out only; they hold no token (the CLIs use the local login).

### 3.3 `fleet/frontier_governor.py` — subscription rate-governor (home-side)
Spend is **not** a constraint (owner is on Codex **Pro 5x** with comfortable resets), so the governor
is **reactive-first**, not a tight rationer. `FrontierGovernor(account, *, min_gap_seconds,
window_seconds=None, window_budget=None)`:
- `allow() -> bool` — true unless the account is currently **tripped** (a recent limit signal) or
  inside the `min_gap` since the last call. The `window_budget` is an **optional high safety bound**
  (default `None` = unbounded; the owner can cap it if running on a shared dev account).
- `record(outcome)` — `outcome='limit'` from a `SubscriptionUnavailable` **trips the account out for
  the rest of the window** (then auto-clears); `'ok'` just stamps the gap.
State persists in a small local sqlite/JSON so a trip survives a process restart within a window. One
instance per account (Codex; Claude). Its jobs, in priority order: (a) **don't hammer after a limit**
(the abuse pattern — the real safety value), (b) pace serial calls with a light gap, (c) *optionally*
bound spend only if the owner asks (e.g. when sharing the dev account). It is NOT there to save money.

### 3.4 `fleet/frontier_pass.py` — orchestrator
`run_frontier_pass(*, sqlite_conn, limit, floor, mode, hours, urls, resume_text, preference_profile,
kg_prompt, use_subscription=True, metered_provider, cross_check_opus=False) -> dict`:
1. `select_priority(...)`.
2. For each job (serial, gap-jitter): build the score prompt; pick the **Codex model by tier**
   (`top_model` e.g. gpt‑5.5 when `cheap_score >= top_tier_floor`, else `backlog_model` — a
   smaller/faster Codex model); pick the backend — `use_subscription AND codex_governor.allow()` →
   `score_via_codex(model=...)`; on `SubscriptionUnavailable` (or governor deny) → **failover** to
   `scorer.score_job(provider=metered_provider)` (the metered frontier API). Record which backend +
   model actually produced the score.
3. If `cross_check_opus` AND the job is in the top tier AND `claude_governor.allow()`: also
   `score_via_claude` and store `opus_score`. (Attended; default off.)
4. `agreement = round(1 - abs(frontier_score - cheap_score)/9.0, 3)` (and an opus/frontier agreement
   when present). Upsert a `frontier_scores` row.
Returns `{scored, by_subscription, failed_over, cross_checked, disagreements}`.

### 3.5 `frontier_scores` brain side-table (no `jobs` migration)
```sql
CREATE TABLE IF NOT EXISTS frontier_scores (
  url            TEXT PRIMARY KEY,
  cheap_score    REAL,
  frontier_score REAL,
  opus_score     REAL,          -- only when the attended cross-check ran
  frontier_decision TEXT,
  provider       TEXT,          -- the backend that produced frontier_score (post-failover)
  agreement      REAL,          -- vs cheap; low = divergent
  reasoning      TEXT,
  scored_at      TEXT
);
```
Self-contained + advisory; idempotently created on first run.

### 3.6 Disagreement report + CLI
`disagreement_report(sqlite_conn, *, max_agreement=0.8) -> list` — rows with `agreement <
max_agreement`, ordered asc — the owner-review queue. CLI `applypilot-fleet-frontier`:
`--mode backlog|new|urls`, `--limit N`, `--floor`, `--top-model`/`--backlog-model`/`--top-tier-floor`
(the per-tier Codex model dial), `--metered-provider <model>`, `--no-subscription` (force
metered/Flavor-A), `--cross-check-opus` (attended Claude), `--min-gap`/`--window-budget` (governor;
budget optional), `--report`.

## 4. Safety / honesty constraints

- **Advisory only.** Frontier/opus scores live in `frontier_scores`; the `jobs` canonical columns are
  never touched. A flaky CLI call cannot corrupt the brain.
- **Own accounts only.** The CLIs use the user's own local login; no token is read or distributed.
  Pooling a friend's subscription is account-sharing (ToS violation + ban vector) and is structurally
  impossible here.
- **Home box only; serial; governed.** The subscription backends run only where the CLI is logged in,
  one call at a time, paced by the governor — never parallel, never on a cloud worker.
- **Dedicated fleet account — strongly recommended.** Running the fleet on the user's *dev* ChatGPT
  (Pro) or Claude (Max) account shares both the quota and the **ban blast radius** with the IDE the
  user codes in (Codex; Claude Code). A **dedicated ChatGPT account for the fleet's Codex backend**
  isolates both. This is a deployment choice (the backend uses whatever login is active), surfaced as
  guidance, not enforced in code.
- **ToS reality, stated.** The metered API (failover/default) is clean. Codex-subscription automation
  is a documented **gray zone** (OpenAI's automation clause) — bounded by the governor, home-box,
  owner-run. Claude-subscription bulk automation is **stricter/riskier** (Consumer Terms +
  OpenClaw) — hence Opus is an **optional, attended, top-tier-only cross-check**, off by default. The
  spec does not claim either subscription path is blessed.
- **Don't starve the dev tools.** The governor's conservative default budget leaves headroom so the
  fleet doesn't consume the quota the user needs for interactive Codex/Claude — the single most
  likely day-to-day harm.

## 5. Testing
- **Selector:** priority order, floor, exclusions (dup, already-frontier-scored); `new`/`urls`.
- **CLI backends (stubbed subprocess):** parse a valid schema object; retry→`SubscriptionUnavailable`
  on malformed; raise on non-zero exit and on a quota/limit signal; assert the `--output-schema`/`-o`
  argv (not `--json`); the Claude variant requires the OAuth-token / no-API-key / no-`--bare` precond.
- **Governor:** `allow()` false past the window budget and within the min-gap; a `record('limit')`
  trips the account out for the rest of the window; state survives a restart.
- **Orchestrator (stubs):** subscription path records `by_subscription`; a `SubscriptionUnavailable`
  (or governor deny) **fails over** to the stubbed metered model and records `provider` accordingly;
  `cross_check_opus` stores `opus_score` only for the top tier; `agreement` correct; advisory-only.
- **Report:** only `agreement < max`, ordered.
- **Opt-in live smokes:** one real `codex exec` (gated on `codex login status` == ChatGPT); one real
  metered-API call (key-gated); one real `claude -p` cross-check (gated on a Max OAuth token).

## 6. Decided
- Subscription is the PRIORITY for the non-bulk frontier lane: Codex primary, metered API
  failover/default, Claude/Opus optional attended cross-check; DeepSeek = bulk floor; home-side pass;
  advisory `frontier_scores`. **Decided.**
- Owner is **Codex Pro 5x** with comfortable resets; **spend is not a constraint**. Governor is
  **reactive-first** (trip + fail over on a real limit signal; `window_budget` optional/unbounded by
  default), not a rationer. **Decided.**
- **Per-tier model dial:** gpt‑5.5 for the top tier (`cheap_score >= top_tier_floor`), a
  smaller/faster Codex model for the broader backlog (higher per-window caps → more coverage).
  Defaults set at build time against `codex exec --help`; tunable per run. **Decided.**
- Opus cross-check = fully **opt-in** (`--cross-check-opus`), attended, default off. **Decided.**
- Remaining **operational** choice (not code-blocking): dedicated fleet ChatGPT account vs running on
  the dev account — a **ban-blast-radius** decision (a ToS action on the dev account would take out
  the Codex/Claude Code IDE), independent of spend. Recommended: dedicated. Runbook will cover the
  chosen path; the backend code is identical either way.
