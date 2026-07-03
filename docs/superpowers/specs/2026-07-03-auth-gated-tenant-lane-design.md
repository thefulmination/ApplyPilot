# Auth-gated ATS tenant lane (supervised → earned autonomy) — design

**Approved:** 2026-07-03 (owner chose "supervised-first, then earned autonomy"; design approved
verbatim). **Problem (audit 7/02 + fleet run 7/03):** 653 gate-passing offsite jobs — including
**94 of the owner's 170 res_build-kept jobs** — are structurally refused because their ATS
requires a login (`config.is_auth_gated_application()`, config.py:382, driven by sites.yaml
`auth_gated`). They are concentrated at marquee employers (Workday tenants: RBC, Adobe, FIS,
BMO, CIBC, TD…). Today "auth-gated" means "never", with no path to yes.

## Principles
- **Sessions are the credential.** The owner logs into each tenant ONCE in the persistent
  home-box Chrome profile (the exact pattern already proven for LinkedIn auth). No ATS
  passwords in profile.json, the DB, env, or logs — ever.
- **Home-lane only, structurally.** Fleet workers never receive auth-gated jobs: the fleet push
  SELECT keeps its existing auth-gated exclusion untouched. The ONLY consumer of tenant status
  is the home apply path. Lane isolation is by construction, not configuration.
- **Graduated trust, owner-flipped.** supervised → trusted is a human decision (one CLI
  command), gated on evidence (≥3 clean supervised submits). No auto-promotion.

## Components

### 1. Tenant registry — brain table `ats_tenants` (database.py, Python-owned, additive)
```sql
CREATE TABLE IF NOT EXISTS ats_tenants (
    host           TEXT PRIMARY KEY,     -- e.g. 'rbc.wd3.myworkdayjobs.com'
    status         TEXT NOT NULL DEFAULT 'excluded',  -- excluded | supervised | trusted
    clean_submits  INTEGER NOT NULL DEFAULT 0,
    failed_submits INTEGER NOT NULL DEFAULT 0,
    daily_cap      INTEGER NOT NULL DEFAULT 5,
    halted_until   TEXT,                 -- same-day halt stamp (ISO); NULL = not halted
    last_result    TEXT,
    updated_at     TEXT
);
```
Unknown hosts behave as `excluded` (today's behavior is the default; zero-row table = zero
behavior change). Helper `tenant_status(host) -> str` with the sites.yaml auth_gated check
unchanged upstream of it.

### 2. CLI tenant ops (cli.py)
- `applypilot tenants` — list: host, status, clean/failed, cap, halted, eligible-job count
  (join on jobs by application-url host).
- `applypilot tenants set <host> supervised|trusted|excluded` — the owner flip. Promoting to
  `trusted` with `clean_submits < 3` requires `--force` (prints why).
- `applypilot tenants halt <host>` / auto-halt (see §5) sets `halted_until` end-of-day.

### 3. Supervised mode (apply path)  [AMENDED 2026-07-03 — owner decision]
`applypilot apply --auth-gated [--tenant <host>] [--limit N]` (home box, owner present):
- Candidate filter: normal apply-eligibility PLUS `is_auth_gated_application(url)` true PLUS
  tenant status `supervised` or `trusted` PLUS not halted PLUS under daily cap.
- Browser runs HEADED using the owner's persistent profile (the LinkedIn profile-clone
  mechanism, pointed at the same profile so tenant sessions persist).
- **NO confirm-before-submit pause.** The owner chose (2026-07-03) to drop the pause-and-confirm
  gate in favor of a full headed apply the owner watches and can Ctrl-C to abort — the one-shot
  agent architecture (subprocess → stdout-to-EOF) made a mid-agent pause+resume brittle, and the
  owner's physical presence + Ctrl-C is the real safety control. The agent applies FULLY,
  including the final submit, exactly like a normal apply. The RESULT-line contract is unchanged
  (`RESULT:APPLIED` only after a real submit).
- After each auth-gated apply's REAL terminal result, `record_submit(host, ok=(status=='applied'),
  result=status)` updates the tenant's counters — `clean_submits` reflects REAL successful
  submits ONLY (never a keystroke), which is what gates graduation to `trusted`. record_submit is
  called EXACTLY ONCE per apply, at the real terminal, never speculatively.
- What now distinguishes `supervised` from `trusted`: **supervised** = headed + owner launched it
  by hand via `apply --auth-gated` (mode=supervised eligible set {supervised,trusted}); **trusted**
  = eligible for the UNATTENDED home loop (§4). Graduation = 3 real clean submits, owner-flipped.
- If the tenant session is dead (login wall detected mid-apply), §5's same-day halt fires for that
  tenant; the tool never attempts credential entry itself.

### 4. Trusted mode
Trusted tenants' jobs flow through the NORMAL home supervised-apply loop (`supervise-apply` /
home lane) automatically: the home acquire filter changes from `NOT is_auth_gated(url)` to
`NOT is_auth_gated(url) OR tenant_status(host) == 'trusted'`. Headless as usual, existing
inter-job throttle, plus the per-tenant daily cap and halt checks. Fleet PG push is untouched.

### 5. Safety rails
- **Per-tenant same-day halt** on any CAPTCHA/challenge/login-wall during apply (these are
  logged-in, identity-tied accounts): sets `halted_until`, skips the tenant's remaining jobs,
  prints loudly. Manual `tenants set` can lift early.
- Per-tenant `daily_cap` (default 5) enforced at candidate selection, counting submits since
  UTC midnight from the applications ledger (implementation uses UTC-day, not local — see Task 1).
- Never-double-apply guards unchanged (dedup_key + applied_at checks run before the tenant
  filter). Every real terminal outcome updates the tenant's clean/failed counters via
  `record_submit` (§3) for audit + trust graduation.

## Error handling
- `--auth-gated` with zero supervised/trusted tenants → exits 0 listing the excluded tenants
  with the enable command (never silently does nothing).
- Halted tenant encountered mid-run → skip remaining jobs for that host, continue others.
- `ats_tenants` table absent (old brain) → all hosts excluded (graceful, same as today).

## Testing
1. Registry: unknown host = excluded; set/list round-trip; trusted-without-evidence needs --force.
2. Candidate filter: auth-gated job excluded by default; included under supervised/trusted;
   halted and over-cap tenants excluded; non-auth-gated jobs unaffected (regression).
3. Confirm gate: `n` records failed_submit and does NOT submit (agent transcript assertion);
   `y` path increments clean_submits (stubbed agent).
4. Same-day halt: challenge mid-run halts host, other hosts continue.
5. Fleet isolation: fleet push SELECT still excludes auth-gated regardless of tenant status.
6. Daily cap: 5 submits → 6th candidate for that host filtered out.

## Success criteria
- Owner can enable one Workday tenant, supervise 3 applies (~minutes each), flip it trusted,
  and have that tenant's remaining kept jobs apply through the normal home loop with caps.
- The 94 res_build-kept auth-gated jobs become reachable tenant-by-tenant, with zero change to
  fleet workers or the offsite lane.

## Non-goals
No account-creation automation; no Workday API; no session distribution to fleet boxes; no
change to LinkedIn lane semantics.
