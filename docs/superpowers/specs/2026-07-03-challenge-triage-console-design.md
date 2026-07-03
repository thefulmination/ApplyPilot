# Auth-challenge triage (console page + CLI, token-secured) — design

**Approved:** 2026-07-03 (owner selected "Console page + CLI" over CLI-only and read-only
alternatives). **Problem (audit 7/02):** 158 open `auth_challenge` rows and 136 jobs parked as
`apply_status='challenge_pending'` (lease-held to 2036) accumulate with NOTHING surfacing them —
the console shows only a count. Every parked job is a scored, gate-passing application the fleet
already paid to reach. Separately, the console has ZERO auth while exposing mutation endpoints
on the LAN (audit finding) — this build adds mutations, so it must bring the token with it.

## Non-goals
- No captcha auto-solving, no remote-assist flow, no screenshot capture changes.
- No changes to park/halt semantics: `queue.resolve_challenge` (both lanes) is the ONLY
  mutation primitive, and LinkedIn halt state is untouched (resolve never clears halts).
- No auth on GET endpoints (read-only, LAN-bound as today).

## Components

### 1. Console mutation token (fleet/console_app.py + launcher)
- `APPLYPILOT_CONSOLE_TOKEN` env var. If unset at console start, generate one
  (`secrets.token_urlsafe(24)`), keep it in-process, and print the full open-me URL
  (`http://<lan-ip>:8787/?token=...`) ONCE at startup (the launcher already prints the URL line).
- EVERY `do_POST` requires the token via `X-Console-Token` header OR `console_token` cookie;
  mismatch/missing → 401 JSON `{"error":"bad token"}`. Constant-time compare (`hmac.compare_digest`).
- `GET /?token=<t>` sets the `console_token` cookie (HttpOnly; no Secure — plain-HTTP LAN app)
  then redirects to `/` — one tap from the printed URL arms the phone browser.
- Existing POST endpoints (pause/resume/etc.) gain the same check — that IS the audit fix; the
  console's mutation surface goes from open-LAN to token-gated in one place (a single decorator/
  helper `_check_token(handler) -> bool` used by `do_POST` before routing).

### 2. `GET /api/challenges` (fleet/console_app.py)
One JSON payload uniting both sources of truth, keyed by url:
- open challenges: `SELECT ... FROM auth_challenge WHERE resolved_at IS NULL`
  (kind, machine_owner, screenshot_url, raised_at).
- parked jobs: `apply_queue` + `linkedin_queue` rows with `apply_status='challenge_pending'`
  (url, lane, company, title, score, updated_at) — parked rows WITHOUT a challenge row still
  appear (the 136 include reclaim-quarantine parks that never raised a challenge).
Response: `{"groups": [{"kind": ..., "host": ..., "rows": [...]}], "counts": {...}}`, grouped
kind → host, rows carry `lane` so actions route to the right queue.

### 3. `POST /api/challenge-action` (fleet/console_app.py)
Body `{"url": ..., "lane": "ats"|"linkedin", "action": "requeue"|"skip"}` (token-gated):
- `requeue` → `queue.resolve_challenge(conn, url, requeue=True)` (ats) or the linkedin
  variant — the existing status-guarded, idempotent primitives; returns rows affected.
- `skip` → same with `requeue=False` (job goes terminal-skip per existing semantics).
- Either way: `UPDATE auth_challenge SET resolved_at=now(), outcome=%s WHERE url=%s AND
  resolved_at IS NULL` (outcome `solved` for requeue, `skipped` for skip).
- Response echoes `{"url", "action", "queue_rows": n, "challenges_closed": m}`; acting on an
  already-resolved row is a no-op success (idempotent), not an error.
- Group action: `{"host": ..., "lane": ..., "action": "skip"}` applies to every open row on
  that host+lane (bounded server-side to 200 rows/request).

### 4. Challenges page (console HTML, same single-file style)
Table grouped kind → host; each row: company/title, age, machine, [Open job] (target=_blank to
the url), [Re-queue] (label: "I solved it"), [Skip]. Group header: count + [Skip all on host].
Buttons POST with the cookie token; on 401 the page shows "open the ?token= URL from the
console window". Mobile-first like the rest of the console (the owner triages from his phone).

### 5. CLI parity (fleet/apply_home_main.py + linkedin_home_main.py)
`challenges --grouped`: kind × host counts table (both mains). The per-row `challenges` /
`resolve-challenge <url> [--skip]` commands already exist and are unchanged.

## Error handling
- DB blip on GET → 503 JSON, page shows a retry banner (matches existing console behavior).
- Unknown lane/action → 400. URL not found in either queue → `queue_rows: 0` (visible no-op).
- Token generation only when BOTH env unset and no token cached — restart invalidates old
  cookies (acceptable; the printed URL re-arms).

## Testing (pytest; PG paths use the disposable `fleet_db` fixture from conftest.py)
1. POST without/with-wrong token → 401 and NO db change; with token → acts.
2. requeue round-trip: park a job (`apply_status='challenge_pending'`), POST requeue → row
   queued again + auth_challenge resolved `solved`; second POST → idempotent no-op success.
3. skip round-trip → terminal skip + outcome `skipped`.
4. Lane isolation: acting on an ats url never touches a linkedin row with the same url and
   vice versa; LinkedIn halt state unchanged by resolve (assert the halt row untouched).
5. GET /api/challenges: parked-without-challenge row appears; grouping correct.
6. Page smoke: GET / renders with the Challenges section; `?token=` sets cookie + redirects.

## Success criteria
- The owner can, from his phone, see all 158+136 items grouped and disposition any of them in
  two taps, with every mutation token-gated.
- Zero new mutation primitives: all writes route through `resolve_challenge` variants +
  the one `auth_challenge` stamp.
