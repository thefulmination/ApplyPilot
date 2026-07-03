# Mac Offsite-Apply Fleet Worker — Design

**Date:** 2026-07-03
**Status:** Implemented (see docs/superpowers/plans/2026-07-03-mac-fleet-worker.md); owner go-live via docs/fleet-mac-worker-runbook.md
**Scope:** One new machine — a macOS computer at a different physical location/LAN — joins the fleet as an **offsite-ATS apply worker only**.

## Goal

An opportunistic apply worker on a Mac that, whenever the machine happens to be on, connects over Tailscale to the fleet Postgres on the home box, leases queued offsite-ATS applies, runs them with the owner's Claude + DeepSeek API keys, and reports results through the existing PG coordination layer. When the Mac is off, the fleet routes around it (existing lease/reclaim + watchdog). The worker self-updates from the private GitHub repo with no action from the Mac's day-to-day user.

## Decisions made (owner-confirmed)

| Decision | Choice |
|---|---|
| Machine role | Apply worker only (offsite ATS lane). No discovery, no scoring, no brain. |
| Machine | A Mac at a different location/LAN, used by another person; variable uptime ("leave it on when possible; worker does its thing whenever it's on"). |
| PG connectivity | Tailscale mesh VPN between home box and Mac. |
| Agent auth | Owner's Anthropic API key + DeepSeek API key, provisioned as env vars on the Mac. |
| Setup approach | **Approach A:** native macOS setup script + launchd wrapper with git-pull auto-update. (Rejected: PowerShell Core port — Windows-assumption fighting; Docker — registry/image ops overhead and always-on Docker Desktop on a personal Mac.) |
| LinkedIn | **Excluded.** Lane B's one-IP hard gate keeps all LinkedIn activity on the home IP. No LinkedIn credentials, profile-clone, or lane-B code paths on the Mac. |

## Architecture

```
Her Mac (different LAN)                     Home box (Dell G15)
┌──────────────────────────┐   Tailscale   ┌──────────────────────────┐
│ launchd LaunchAgent      │  (100.x/10)   │ Postgres 18              │
│  └─ wrapper (update loop)│──────────────▶│  applypilot_fleet DB     │
│      └─ applypilot-fleet-│   fleet_worker│  (apply_queue, leases,   │
│         worker --label   │   role, pw    │   fleet_config, canary,  │
│         mac-0            │               │   rate governor …)       │
│ Claude CLI + Playwright  │               │ SQLite brain (unchanged) │
│ + Chrome                 │               │ Watchdog / Doctor /      │
│ ~/.applypilot (profile,  │               │ Console :8787            │
│  resume, env chmod 600)  │               └──────────────────────────┘
└──────────────────────────┘
        │  git fetch (read-only deploy key)
        ▼
  Private GitHub repo (ApplyPilot)
```

Workers are stateless: all coordination state lives in PG, all job/outcome truth in the home brain. The Mac holds only code, config, and keys.

## Components

### 1. Networking + PG hardening (one-time, home box)

- Install Tailscale on the home box and the Mac under the owner's tailnet.
- Postgres changes:
  - `listen_addresses` extended to include the home box's Tailscale address.
  - `pg_hba.conf`: allow only the tailnet range `100.64.0.0/10`, database `applypilot_fleet`, role `fleet_worker`, `scram-sha-256`.
  - New role `fleet_worker`: real password, DML (SELECT/INSERT/UPDATE/DELETE) on `applypilot_fleet` tables only; no superuser, no other databases. The owner's passwordless pgpass DSN remains local-only.
- Windows Firewall: inbound 5432 allowed **on the Tailscale interface only**.
- Side benefit: the LAN-only fleet console (`:8787`) becomes reachable from any tailnet device.

Deliverable: a home-box hardening script (PowerShell) that applies the PG config, creates the role, prints the keyword-format DSN for the Mac.

### 2. Mac install — `setup-mac-worker.sh` (run once, interactive)

Installs and configures:
- Homebrew, Python 3.11, Node LTS, git (Xcode CLT), Google Chrome, Tailscale, Claude Code CLI (`npm i -g`).
- Clone of the private repo via a **read-only GitHub deploy key** (`~/.ssh/applypilot_deploy`) — the owner's GitHub credentials never live on the Mac.
- Dedicated venv + `pip install -e .` (editable install; console scripts like `applypilot-fleet-worker` become available).
- Playwright browser install.
- `~/.applypilot/`: owner's `profile.json`, base resume (applied **as-is** — no tailoring, per standing policy), and `fleet-worker.env` (`chmod 600`) containing:
  - `APPLYPILOT_FLEET_DSN` / `FLEET_PG_DSN` (keyword format, `fleet_worker` role, Tailscale address)
  - `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`
  - `APPLYPILOT_DIR`, `CLAUDE_PATH`, worker label prefix `mac`

**Secrets rule:** the script prompts for keys interactively at install time. Keys are never embedded in the script, a bundle, a zip, or anything that transits OneDrive (lesson from the machine2-bundle cleartext-password incident).

**Kill switch (all owner-side):** rotate the two API keys, change/drop the `fleet_worker` PG password, remove the Mac from the tailnet.

### 3. Runtime — launchd wrapper with auto-update

- `~/Library/LaunchAgents/com.applypilot.fleetworker.plist`: `RunAtLoad` + `KeepAlive`. The worker is alive whenever the Mac is on/logged in; crashes restart it. This is the "opportunistic presence" behavior — no scheduling logic needed.
- The agent runs `run-worker-mac.sh` (wrapper), which:
  1. On start and every ~6 h: `git fetch` via deploy key; if the designated production branch advanced, update to its head. (The production branch is pinned in the env file — confirm which branch is live during implementation; the repo has feature branches that must not auto-deploy.)
  2. **Updates apply only between jobs, never mid-apply.** An interrupted apply is the "may-have-submitted" double-apply vector; the wrapper waits for worker idle / uses the worker's graceful-stop path (verify in porting audit) before restarting.
  3. Re-runs `pip install -e .` only when `pyproject.toml` changed; otherwise the editable install picks up code on restart.
  4. Starts `applypilot-fleet-worker --label mac-0` with the env file sourced.
  5. On update failure: keep running current code, log the failure.
- Logs: `~/applypilot/logs/worker-mac-0.log` (existing human-transcript format).

### 4. Porting audit (before go-live)

Targeted sweep of the worker code paths for Windows assumptions:
- Path handling (drive letters, `LOCALAPPDATA`, path separators).
- Subprocess invocations of `.cmd`/`.ps1` shims (the `tsx.cmd` class of DOA bug) — Claude/Codex CLI resolution on macOS.
- Anything else that assumes PowerShell or Windows services.
- Confirm: Chrome profile-clone machinery is LinkedIn-lane-only and is fully skipped for offsite-ATS applies.
- Determine the worker's graceful-stop mechanism (needed by wrapper step 2); if none exists, add a minimal "finish current job then exit" signal handler.

### 5. Safety & failure modes

All central rails apply automatically because every mutation flows through PG:
canary auto-pause, cost hard lease guards, approval gates, status-passthrough (never phantom-apply), cross-lane dedup.

| Failure | Behavior |
|---|---|
| Mac shuts down / sleeps mid-lease | Watchdog reclaims the lease (existing Layer A). |
| Tailscale down / home box unreachable | Worker idles, retries with backoff. It cannot mutate anything locally (workers never write SQLite). |
| Git update fails | Wrapper keeps running current code; logs. |
| Her Mac compromised / needs cutoff | 3-step owner-side revocation (keys, PG password, tailnet). |
| Home box down (known Dell G15 hardware resets) | Entire fleet stalls — **pre-existing dependency, unchanged by this project.** Moving PG off the failing laptop is a separate future project. |

### 6. Go-live (canary runbook)

1. Prereq: revive the fleet on the home box (tasks unregistered since 6/30 — `register-fleet-tasks.ps1`) and confirm queue has work.
2. Load a Mac-specific canary: small reserve (~5 applies) via the existing canary-loader pattern.
3. Watch live on the fleet console over Tailscale; verify leases, applies, and result statuses land correctly under the `mac-0` label.
4. Raise caps to the remote HIGH profile once canary is clean.

## Deliverables

All in the Python ApplyPilot repo:
1. `setup-mac-worker.sh` — one-time interactive Mac installer.
2. `run-worker-mac.sh` — launchd wrapper (update loop + worker supervision).
3. `com.applypilot.fleetworker.plist` — LaunchAgent template.
4. Home-box PG remote-access hardening script (PowerShell).
5. Porting-audit fixes (+ graceful-stop handler if missing).
6. `docs/mac-worker-runbook.md` — owner-facing install + canary runbook.

## Out of scope

- LinkedIn lane on the Mac (excluded by design).
- Discovery/scoring on the Mac.
- Moving PG or the brain off the home box.
- `fleet_desired_state`-driven "update now" push (v2 candidate; v1 is pull-on-start + 6 h poll).
