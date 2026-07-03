# Challenge triage — runbook

The fleet parks jobs it can't finish because a login wall or CAPTCHA blocked the apply
(`apply_status='challenge_pending'`, lease-held far into the future). Nothing used to surface
them; this triage lets you clear them from your phone.

**Live as of 2026-07-03:** 173 open challenges (105 login_gate + 68 visible_captcha),
172 parked apply-lane jobs + 1 LinkedIn — every one a scored, gate-passing application the
fleet already paid to reach.

## From the console (phone-friendly)

1. Restart the console launcher: `.\run-fleet-console.ps1` (home box). It prints TWO URLs — the
   plain LAN URL and an **arm URL that ends in `?token=…`**.
2. Open the **`?token=` URL once** on your phone (same LAN/Tailscale). That sets the
   `console_token` cookie; every button after that is authorized. All mutations are token-gated —
   a device without the cookie can view but not act.
3. Scroll to the **Challenges** card. Rows are grouped `kind · host · count`. Per row:
   - **Open job** — opens the posting in a new tab so you can eyeball it.
   - **I solved it → Re-queue** — you cleared the wall (logged in / passed the CAPTCHA in your
     own browser); the job goes back in the queue for the fleet to retry.
   - **Skip** — give up on this one; it goes terminal-skip (retained, not deleted).
   - **Skip all on host** — group header; clears up to 200 at once on a hostile host.
4. If a button ever shows *"token expired"*, re-open the `?token=` URL from step 2.

## From the CLI

```powershell
# a kind × host count view per lane (no browser)
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe    challenges --grouped
.\.conda-env\Scripts\applypilot-fleet-linkedin-home.exe challenges --grouped
# act on one url (unchanged, pre-existing):
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe resolve-challenge <url>          # re-queue
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe resolve-challenge <url> --skip   # skip
```

## Safety properties
- **LinkedIn isolation is structural.** An apply-lane action never touches a LinkedIn row of the
  same url and vice versa; LinkedIn halt state is never cleared by a resolve.
- **One mutation primitive.** Every action routes through the existing `resolve_challenge` /
  `resolve_linkedin_challenge` — status-guarded and idempotent (acting on an already-cleared row
  is a harmless no-op).
- **Token-gated writes, open reads.** Viewing is open on the LAN; every mutation requires the
  cookie/`X-Console-Token` (this also closed the audit's "console has no auth" finding for the
  pre-existing pause/resume/cap actions).
