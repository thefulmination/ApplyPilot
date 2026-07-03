# Auth-gated tenant lane — runbook

Login-walled ATS tenants (Workday, Amazon, Oracle, ADP, Eightfold…) are refused by default:
the fleet can't solve a login. This lane makes them applyable **tenant-by-tenant**, on the home
box, with you present — no ATS password is ever stored (your logged-in browser session is the
only credential).

**Live as of 2026-07-03:** 932 gate-passing, un-applied, auth-gated jobs across 264 tenant hosts.
The biggest pools: amazon.jobs (345), adobe.wd5.myworkdayjobs.com (38),
salesforce.wd12.myworkdayjobs.com (26), intel (19), nvidia (18), visa (10), pwc (8), mastercard (7),
fis (6). Enable the tenants you care about; each has its own daily cap.

## The flow (per tenant)

1. **Log in once.** Open the tenant (e.g. Adobe Workday) in the home box's persistent Chrome
   profile — the same profile the apply agent uses — and sign in. That session is the credential.
2. **Enable the tenant, supervised:**
   ```powershell
   .\run-applypilot.ps1 tenants set adobe.wd5.myworkdayjobs.com supervised
   ```
3. **Supervised run — you watch a headed browser:**
   ```powershell
   .\run-applypilot.ps1 apply --auth-gated --tenant adobe.wd5.myworkdayjobs.com --limit 3
   ```
   The agent applies fully (fills + submits) in a **headed** window while you watch; **Ctrl-C
   aborts** at any point. There is no click-to-confirm prompt — your presence + Ctrl-C is the
   control. Each real `RESULT:APPLIED` increments the tenant's `clean_submits`; a failure
   increments `failed_submits`. A CAPTCHA/login-wall **halts that tenant for the rest of the UTC
   day** automatically (these are identity-tied accounts — we don't hammer them).
4. **Check progress:**
   ```powershell
   .\run-applypilot.ps1 tenants          # list: status, clean/failed, cap, halted?, eligible count
   ```
5. **Graduate to trusted** once a tenant has ≥3 clean submits you're happy with:
   ```powershell
   .\run-applypilot.ps1 tenants set adobe.wd5.myworkdayjobs.com trusted
   ```
   (Refuses below 3 clean submits unless you pass `--force`.) **Trusted** tenants then apply
   through your **normal home apply loop** unattended — with the same per-tenant daily cap and
   same-day halt still enforced.

## Safety properties
- **No password is ever stored.** Sessions live in your Chrome profile; the tool never types
  credentials.
- **Fleet workers never see auth-gated jobs.** The fleet push is byte-unchanged — this lane is
  home-box only, structurally.
- **Supervised vs trusted:** `supervised` = headed + you launched it by hand. `trusted` = allowed
  in the unattended home loop. Unknown/excluded tenants are never touched. A normal `apply` run
  (no `--auth-gated`) admits **trusted only**, never supervised.
- **Per-tenant daily cap** (default 5) + **same-day halt on any challenge**, both enforced at
  candidate selection. Lift a halt or change status anytime with `tenants set`.
- **Never double-applies:** dedup + applied_at guards run before the tenant filter.

## Commands
```powershell
tenants                                  # list all tenants
tenants set <host> supervised|trusted|excluded [--force]
tenants halt <host>                      # halt for the rest of the UTC day
apply --auth-gated [--tenant <host>] [--limit N]   # supervised headed run (home box)
```
